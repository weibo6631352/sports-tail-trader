"""当前策略的恢复与修复语义。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING

from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.domain.order import Order, OrderSide, OrderType
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.sports_live import LiveEvent
from polymarket_trader.domain.decisions import DecisionContext, QuantDecision, TradingDecision

from polymarket_trader.workflow.config import TradingWorkflowConfig
from polymarket_trader.workflow.position_plan import cap_price_to_clob_limit, build_position_plan_metadata
from polymarket_trader.workflow.outcomes import describe_sports_market, SportsMarketFamily, tail_token_targets
from polymarket_trader.workflow.tail import LiveGameStatus, live_game_state_from_metadata
from polymarket_trader.workflow.trading.helpers import resolve_tick_size


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

    managed_token_ids = {target.token_id for target in tail_token_targets(context.market)}
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
        if not config.auto_exit_enabled and _is_open_exit_order(order) and not _is_profit_take_exit_order(order):
            actions.append(
                TradingDecision.cancel(
                    reason="settlement_only_open_exit_order_detected",
                    token_id=order.token_id,
                    order_id=order_id,
                    market_slug=order.market_slug or context.market.market_slug,
                    metadata=recovery_metadata,
                )
            )

    open_exit_by_token: dict[str, Decimal] = {}
    for order in open_orders:
        if _is_open_exit_order(order):
            open_exit_by_token[order.token_id] = open_exit_by_token.get(order.token_id, Decimal("0")) + (
                _open_order_shares(order)
            )

    for position in positions:
        open_exit_shares = open_exit_by_token.get(position.token_id, Decimal("0"))
        uncovered_shares = position.shares - open_exit_shares
        if uncovered_shares <= Decimal("0"):
            continue
        # 未覆盖持仓不再由恢复侧挂静态价 SELL：动态退出引擎 ``decide_exit``
        # 每个 reconcile 周期都会从实时盘口重估 HOLD/EXIT 并下单。恢复侧若
        # 再挂静态退出单会覆盖持仓份额，使 ``decide_exit`` 因
        # ``no_uncovered_shares`` 跳过、动态引擎被旁路。此处仅保留对历史遗留
        # 高均价仓位的 settlement-only profit-take 补单（_recovery_profit_take_action）。
        if config.auto_exit_enabled and not missing_target:
            continue
        if missing_target and context.market.trading_status != TradingStatus.ELIGIBLE:
            continue
        profit_take_action = _recovery_profit_take_action(
            config,
            context,
            position,
            uncovered_shares=uncovered_shares,
            recovery_metadata=recovery_metadata,
        )
        if profit_take_action is not None:
            actions.append(profit_take_action)

    pause_trading = context.market.trading_status in {
        TradingStatus.PAUSED,
        TradingStatus.CLOSED,
        TradingStatus.RESOLVED,
    } or (
        account_snapshot is not None and account_snapshot.is_market_paused(context.market.condition_id)
    ) or abnormal_pause_reason is not None or missing_target
    return QuantDecision(
        reason="strategy_recovery",
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
    """撤掉超过策略 TTL 的历史开放 BUY，避免旧 GTC 买单长期占用资金。"""

    if not _is_open_entry_order(order):
        return False
    max_resting_seconds = config.tail_entry_maker_max_resting_seconds
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


def _is_open_exit_order(order: Order) -> bool:
    return order.side == OrderSide.SELL and order.open


def _is_profit_take_exit_order(order: Order) -> bool:
    """识别策略恢复侧或入场后跟单生成的 profit-take SELL。"""

    return order.reason in {"strategy_profit_take", "recovery_profit_take"}


def _open_order_shares(order: Order) -> Decimal:
    if order.remaining_shares is not None:
        return max(order.remaining_shares, Decimal("0"))
    if order.size_shares is not None:
        return max(order.size_shares, Decimal("0"))
    return Decimal("0")


def _recovery_profit_take_action(
    config: TradingWorkflowConfig,
    context: DecisionContext,
    position: Position,
    *,
    uncovered_shares: Decimal,
    recovery_metadata: dict[str, object],
) -> TradingDecision | None:
    """给历史遗留的高成本价未覆盖仓位补一张小利润 SELL。

    该路径只处理已经实际持仓的退出，不新增 BUY，也不绕过交易主链路。低均价仓位
    仍保持 settlement-only，避免为了很小价差长期挂出不必要的 SELL。
    """

    if not config.tail_recovery_profit_take_enabled:
        return None  # 功能未开启，跳过止盈补单。
    if position.shares <= Decimal("0") or position.cost_usdc <= Decimal("0"):
        return None  # 仓位数据异常（空仓或零成本），无法计算均价，跳过。
    average_price = position.cost_usdc / position.shares
    if average_price < config.tail_recovery_profit_take_min_avg_price or average_price >= Decimal("1"):
        return None  # 均价过低（结算效率合理，不需要提前止盈）或异常越界。
    target_price, price_source = _recovery_profit_take_price(context, position.token_id, average_price)
    if target_price is None or target_price > Decimal("1"):
        return None  # 无法确定有效止盈价（盘口缺失或价格越界）。
    expected_profit = uncovered_shares * (target_price - average_price)
    if expected_profit < config.tail_profit_take_min_profit_usdc:
        return None  # 预期毛利润低于最小阈值，不值得挂单。
    exit_metadata = dict(recovery_metadata)
    exit_metadata.update(
        build_position_plan_metadata(
            config,
            context,
            token_id=position.token_id,
            source_reason="recovery_profit_take",
            target_size_shares=uncovered_shares,
        )
    )
    exit_metadata.update(
        {
            "exit_mode": "profit_take",
            "exit_source_reason": "recovery_profit_take",
            "profit_take_target_price": str(target_price),
            "profit_take_price_source": price_source,
            "profit_take_expected_profit_usdc": _decimal_metadata_text(expected_profit),
            "recovery_position_avg_price": _decimal_metadata_text(average_price),
        }
    )
    plan = exit_metadata.get("position_plan")
    if isinstance(plan, dict):
        plan["target_exit_price"] = str(target_price)
        plan["primary_action"] = "place_recovery_profit_take_gtc_sell"
        plan["settlement_rule"] = "keep_profit_take_order_until_fill_or_authoritative_resolution"
        plan["recovery_rule"] = "preserve_existing_profit_take_exit_order"
    exit_metadata["exit_target_price"] = str(target_price)
    return TradingDecision.sell(
        reason="recovery_profit_take",
        token_id=position.token_id,
        price=target_price,
        size_shares=uncovered_shares,
        market_slug=position.market_slug or (context.market.market_slug if context.market is not None else None),
        metadata=exit_metadata,
    )


def _recovery_profit_take_price(
    context: DecisionContext,
    token_id: str,
    average_price: Decimal,
) -> tuple[Decimal | None, str]:
    """优先使用当前可成交 bid，否则退回均价上方一档止盈价。"""

    tick_price = _next_tick_price(context, average_price)
    orderbook = _orderbook_for_token(context, token_id)
    best_bid = None if orderbook is None else orderbook.best_bid
    if best_bid is not None and best_bid > average_price and (
        tick_price is None or best_bid > tick_price
    ):
        tick_size = resolve_tick_size(orderbook, context.market)
        return cap_price_to_clob_limit(best_bid, tick_size=tick_size), "best_bid"
    return tick_price, "next_tick"


def _orderbook_for_token(context: DecisionContext, token_id: str):
    """从恢复上下文里找到对应 token 的盘口快照。"""

    if context.orderbook is not None and context.orderbook.token_id == token_id:
        return context.orderbook
    for view in context.market_token_views:
        if view.token_id == token_id:
            return view.orderbook
    return None


def _next_tick_price(context: DecisionContext, price: Decimal) -> Decimal | None:
    """返回当前价格上方一档 tick。"""

    tick_size = resolve_tick_size(context.orderbook, context.market)
    units = (price / tick_size).to_integral_value(rounding=ROUND_CEILING)
    return cap_price_to_clob_limit((units + 1) * tick_size, tick_size=tick_size)


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
    threshold_seconds = config.tail_stale_no_live_state_seconds
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
    """根据已接入的直播状态判断是否需要暂停新增交易。"""

    game = live_game_state_from_metadata(context.metadata)
    if game is None:
        return None
    if game.status in {
        LiveGameStatus.PAUSED,
        LiveGameStatus.POSTPONED,
        LiveGameStatus.CANCELLED,
        LiveGameStatus.DISPUTED,
        LiveGameStatus.RETIRED,
        LiveGameStatus.UNKNOWN,
    }:
        return f"sports_live_state_{game.status.value}"
    if game.status == LiveGameStatus.ENDED:
        # 已结束但 Polymarket 未封盘是当前策略的确定性机会，不按直播异常暂停。
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
        return config.tail_tennis_max_game_state_age_seconds
    if game.baseball_state is not None or league in {"mlb", "baseball"}:
        return config.tail_baseball_max_game_state_age_seconds
    if game.esports_state is not None or (game.sport or "").strip().lower() == "esports":
        return config.tail_esports_max_game_state_age_seconds
    # 足球(J1/J2/各联赛)livescore feed 更新慢(30-60s),用单独 soccer 阈值;
    # 之前缺这个分支导致 recovery 用 default 10s,reconcile 把所有 J2 market 标
    # sports_live_state_stale → pause_trading_for_market → 即使 candidate accepted
    # 也下不了单。
    if game.soccer_state is not None or (game.sport or "").strip().lower() == "soccer" or league in {"j1", "j2", "j1100", "j2100"} or any(k in league for k in ("soccer","football","liga","league","serie")):
        return config.tail_soccer_max_game_state_age_seconds
    # cricket/rugby/handball：Goalserve inplay 不覆盖，仅 livescore getfeed，feed
    # 周期 30-90s。走专属 livescore_only 阈值避免 default 60s 仍然偶发误标 stale。
    sport_text = (game.sport or "").strip().lower()
    if game.cricket_state is not None or game.handball_state is not None or sport_text in {"cricket", "rugby", "handball", "rugbyleague"} or "rugby" in league or "cricket" in league or "handball" in league:
        return config.tail_livescore_only_max_game_state_age_seconds
    return config.tail_max_game_state_age_seconds


def _recovery_metadata(
    config: TradingWorkflowConfig,
    context: DecisionContext,
    *,
    abnormal_pause_reason: str | None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "recovery_reason": abnormal_pause_reason or "strategy_recovery",
    }
    if abnormal_pause_reason is not None:
        metadata["recovery_pause_reason"] = abnormal_pause_reason
        metadata.update(
            build_position_plan_metadata(
                config,
                context,
                token_id=context.token_id,
                source_reason=abnormal_pause_reason,
            )
        )
    return metadata
