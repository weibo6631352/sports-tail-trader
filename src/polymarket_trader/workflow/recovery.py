"""当前量化恢复与修复语义。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.domain.order import Order, OrderSide, OrderType
from polymarket_trader.domain.sports_live import LiveEvent
from polymarket_trader.domain.decisions import DecisionContext, QuantDecision, TradingDecision

from polymarket_trader.workflow.config import TradingWorkflowConfig
from polymarket_trader.workflow.outcomes import describe_sports_market, SportsMarketFamily, sports_token_targets
from polymarket_trader.sports import LiveGameStatus, live_game_state_from_metadata


def build_recovery_quant_decision(
    config: TradingWorkflowConfig,
    context: DecisionContext,
) -> QuantDecision:
    """reconcile_cycle 触发：清理僵尸订单 / 历史 profit-take / pause 信号。

    历史名 ``decide_recovery``，现由 ``quant_decide`` 在 trigger_kind=
    ``reconcile_cycle`` 时调用。返回 QuantDecision 而非旧 RecoveryDecision。
    """
    if context.market is None:
        return QuantDecision(reason="missing_market_state")

    managed_token_ids = {target.token_id for target in sports_token_targets(context.market)}
    missing_target = not managed_token_ids

    account_snapshot = context.account_snapshot
    positions = []
    if context.position is not None and (
        context.position.token_id in managed_token_ids
        or (missing_target and context.position.condition_id == context.market.condition_id)
    ):
        positions.append(context.position)
    if account_snapshot is not None:
        if managed_token_ids:
            for token_id in managed_token_ids:
                position = account_snapshot.get_position(context.market.condition_id, token_id)
                if position is not None and position not in positions:
                    positions.append(position)
        else:
            for position in account_snapshot.positions:
                if position.condition_id == context.market.condition_id and position not in positions:
                    positions.append(position)

    open_orders = tuple(
        order
        for order in context.open_orders
        if order.token_id in managed_token_ids
        or (missing_target and order.condition_id == context.market.condition_id)
    )
    if not open_orders and account_snapshot is not None:
        if managed_token_ids:
            open_orders = tuple(
                order
                for token_id in managed_token_ids
                for order in account_snapshot.open_orders_for_market(context.market.condition_id, token_id)
            )
        else:
            open_orders = tuple(
                order
                for order in account_snapshot.open_orders
                if order.condition_id == context.market.condition_id
            )

    actions: list[TradingDecision] = []
    abnormal_pause_reason = (
        _abnormal_live_state_pause_reason(config, context)
        or _stale_no_live_state_pause_reason(config, context)
    )
    recovery_metadata = _recovery_metadata(config, context, abnormal_pause_reason=abnormal_pause_reason)
    for order in open_orders:
        order_id = _order_identifier(order)
        if order_id is None:
            continue
        if _should_cancel_open_entry_order(config, context, order):
            actions.append(
                TradingDecision.cancel(
                    reason="open_entry_order_detected",
                    token_id=order.token_id,
                    order_id=order_id,
                    market_slug=order.market_slug or context.market.market_slug,
                    metadata=recovery_metadata,
                )
            )
            continue
        if missing_target:
            continue

    # 未覆盖持仓不由恢复侧挂静态价 SELL：动态退出引擎 ``decide_exit`` 每个 reconcile
    # 周期都会从实时盘口重估 HOLD/EXIT 并下单——恢复侧不再补 profit-take 单。

    pause_trading = context.market.trading_status in {
        TradingStatus.PAUSED,
        TradingStatus.CLOSED,
        TradingStatus.RESOLVED,
    } or (
        account_snapshot is not None and account_snapshot.is_market_paused(context.market.condition_id)
    ) or abnormal_pause_reason is not None or missing_target
    return QuantDecision(
        reason="quant_recovery",
        actions=tuple(actions),
        pause_trading=pause_trading,
        pause_reason=abnormal_pause_reason
        or ("missing_target" if missing_target else "")
        or ("market_not_tradable" if pause_trading else ""),
    )


def _order_identifier(order: Order) -> str | None:
    return order.order_id or order.idempotency_key


def _is_open_entry_order(order: Order) -> bool:
    return order.side == OrderSide.BUY and order.open


def _should_cancel_open_entry_order(
    config: TradingWorkflowConfig,
    context: DecisionContext,
    order: Order,
) -> bool:
    """撤掉超过 TTL 的历史开放 BUY，避免旧 GTC 买单长期占用资金。"""

    if not _is_open_entry_order(order):
        return False
    max_resting_seconds = config.entry_maker_max_resting_seconds
    if max_resting_seconds <= 0:
        # max_resting_seconds <= 0 表示"配置：立即撤单"，不是超时。
        return True
    opened_at = order.created_at or order.updated_at
    if opened_at is None:
        return order.order_type != OrderType.GTC
    now = context.now or datetime.now(timezone.utc)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - opened_at).total_seconds() > max_resting_seconds


def _decimal_metadata_text(value: Decimal) -> str:
    """把 Decimal 转成稳定 metadata 文本。"""

    return str(value.quantize(Decimal("0.000000000000000001")).normalize())


def _stale_no_live_state_pause_reason(
    config: TradingWorkflowConfig,
    context: DecisionContext,
) -> str | None:
    """检测「赛事起始已过 stale 阈值但完全无直播状态」的 stale market。

    仅适用于 SINGLE_GAME：outright/series 不依赖直播源，不因缺失直播状态而 pause。
    意味着 market 已经脱离入场窗口、活跃直播源也无法提供数据（赛事结束 /
    联赛不被任何数据源覆盖）。继续保留在 registry 仅是 scanner 噪音；主动
    pause 让 reconcile / settle scanner 把它纳入退订路径。
    """

    market = context.market
    if market is None or market.game_start_time is None:
        return None
    descriptor = describe_sports_market(market)
    if descriptor is None or descriptor.market_family != SportsMarketFamily.SINGLE_GAME:
        return None
    if live_game_state_from_metadata(context.metadata) is not None:
        return None
    threshold_seconds = config.stale_no_live_state_seconds
    if threshold_seconds <= 0:
        return None
    now = context.now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    game_start = market.game_start_time
    if game_start.tzinfo is None:
        game_start = game_start.replace(tzinfo=timezone.utc)
    if (now.astimezone(timezone.utc) - game_start.astimezone(timezone.utc)).total_seconds() <= threshold_seconds:
        return None
    return "stale_no_live_state"


def _abnormal_live_state_pause_reason(
    config: TradingWorkflowConfig,
    context: DecisionContext,
) -> str | None:
    """根据已接入的直播状态判断是否需要暂停新增交易。

    PAUSED 从异常集中移除——Goalserve <core stopped="1"> 在 CS2 round/buy phase
    等正常节奏中频繁瞬态触发,把它当 abnormal 会导致 reconcile 反复 pause/resume
    market,真停赛 / 弃赛 / 改判 走 POSTPONED / CANCELLED / DISPUTED / RETIRED,
    这些仍 block.
    """

    game = live_game_state_from_metadata(context.metadata)
    if game is None:
        return None
    if game.status in {
        LiveGameStatus.POSTPONED,
        LiveGameStatus.CANCELLED,
        LiveGameStatus.DISPUTED,
        LiveGameStatus.RETIRED,
        LiveGameStatus.UNKNOWN,
    }:
        return f"sports_live_state_{game.status.value}"
    if game.status == LiveGameStatus.ENDED:
        # 已结束但 Polymarket 未封盘是当前量化确定性机会，不按直播异常暂停。
        return None
    if game.observed_at is not None and _live_state_age_seconds(context, game.observed_at) > (
        _max_live_state_age_seconds(config, game)
    ):
        return "sports_live_state_stale"
    return None


def _live_state_age_seconds(context: DecisionContext, observed_at: datetime) -> float:
    current_time = context.now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return (current_time.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()


def _max_live_state_age_seconds(config: TradingWorkflowConfig, game: LiveEvent) -> int:
    """按运动项目选择恢复侧的新鲜度窗口。"""

    league = game.league.strip().lower()
    if game.tennis_state is not None or "tennis" in league or league in {"atp", "wta"}:
        return config.tennis_max_game_state_age_seconds
    if game.baseball_state is not None or league in {"mlb", "baseball"}:
        return config.baseball_max_game_state_age_seconds
    if game.esports_state is not None or (game.sport or "").strip().lower() == "esports":
        return config.esports_max_game_state_age_seconds
    # 足球(J1/J2/各联赛)livescore feed 更新慢(30-60s),用单独 soccer 阈值;
    # 之前缺这个分支导致 recovery 用 default 10s,reconcile 把所有 J2 market 标
    # sports_live_state_stale → pause_trading_for_market → 即使 candidate accepted
    # 也下不了单。
    if game.soccer_state is not None or (game.sport or "").strip().lower() == "soccer" or league in {"j1", "j2", "j1100", "j2100"} or any(k in league for k in ("soccer","football","liga","league","serie")):
        return config.soccer_max_game_state_age_seconds
    # cricket/rugby/handball：Goalserve inplay 不覆盖，仅 livescore getfeed，feed
    # 周期 30-90s。走专属 livescore_only 阈值避免 default 60s 仍然偶发误标 stale。
    sport_text = (game.sport or "").strip().lower()
    if game.cricket_state is not None or game.handball_state is not None or sport_text in {"cricket", "rugby", "handball", "rugbyleague"} or "rugby" in league or "cricket" in league or "handball" in league:
        return config.livescore_only_max_game_state_age_seconds
    return config.default_max_game_state_age_seconds


def _recovery_metadata(
    config: TradingWorkflowConfig,
    context: DecisionContext,
    *,
    abnormal_pause_reason: str | None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "recovery_reason": abnormal_pause_reason or "quant_recovery",
    }
    if abnormal_pause_reason is not None:
        metadata["recovery_pause_reason"] = abnormal_pause_reason
        if context.market is not None:
            metadata["condition_id"] = context.market.condition_id
            metadata["market_slug"] = context.market.market_slug
        if context.token_id:
            metadata["token_id"] = context.token_id
    return metadata
