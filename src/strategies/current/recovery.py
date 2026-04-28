"""当前策略的恢复与修复语义。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.domain.order import OrderSide
from polymarket_trader.extension_api import RecoveryDecision, ExtensionContext, ExtensionDecision

from strategies.current.config import CurrentStrategyConfig
from strategies.current.exit_plan import build_exit_plan_metadata, exit_price_for_context
from strategies.current.outcomes import sports_token_targets
from strategies.current.sports_tail import LiveGameStatus, live_game_state_from_metadata


def decide_recovery(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> RecoveryDecision:
    if context.market is None:
        return RecoveryDecision(reason="missing_market_state")

    managed_token_ids = {target.token_id for target in sports_token_targets(context.market)}
    if not managed_token_ids:
        return RecoveryDecision(
            reason="missing_sports_target",
            pause_trading=True,
            pause_reason="missing_sports_target",
        )

    account_snapshot = context.account_snapshot
    positions = []
    if context.position is not None and context.position.token_id in managed_token_ids:
        positions.append(context.position)
    if account_snapshot is not None:
        for token_id in managed_token_ids:
            position = account_snapshot.get_position(context.market.condition_id, token_id)
            if position is not None and position not in positions:
                positions.append(position)

    open_orders = tuple(order for order in context.open_orders if order.token_id in managed_token_ids)
    if not open_orders and account_snapshot is not None:
        open_orders = tuple(
            order
            for token_id in managed_token_ids
            for order in account_snapshot.open_orders_for_market(context.market.condition_id, token_id)
        )

    actions: list[ExtensionDecision] = []
    abnormal_pause_reason = _abnormal_live_state_pause_reason(config, context)
    recovery_metadata = _recovery_metadata(config, context, abnormal_pause_reason=abnormal_pause_reason)
    for order in open_orders:
        order_id = _order_identifier(order)
        if order_id is None or not _is_open_entry_order(order):
            continue
        actions.append(
            ExtensionDecision.cancel(
                reason="open_entry_order_detected",
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
        if uncovered_shares > Decimal("0"):
            exit_metadata = dict(recovery_metadata)
            exit_metadata.update(
                build_exit_plan_metadata(
                    config,
                    context,
                    token_id=position.token_id,
                    source_reason="recovery_exit_shortage",
                    target_size_shares=uncovered_shares,
                )
            )
            actions.append(
                ExtensionDecision.sell(
                    reason="recovery_exit_shortage",
                    token_id=position.token_id,
                    price=exit_price_for_context(config, context),
                    size_shares=uncovered_shares,
                    market_slug=position.market_slug or context.market.market_slug,
                    metadata=exit_metadata,
                )
            )

    pause_trading = context.market.trading_status in {
        TradingStatus.PAUSED,
        TradingStatus.CLOSED,
        TradingStatus.RESOLVED,
    } or (
        account_snapshot is not None and account_snapshot.is_market_paused(context.market.condition_id)
    ) or abnormal_pause_reason is not None
    return RecoveryDecision(
        reason="strategy_recovery",
        actions=tuple(actions),
        pause_trading=pause_trading,
        pause_reason=abnormal_pause_reason or ("market_not_tradable" if pause_trading else ""),
    )


def _order_identifier(order) -> str | None:
    return order.order_id or order.idempotency_key


def _is_open_entry_order(order) -> bool:
    return order.side == OrderSide.BUY and order.open


def _is_open_exit_order(order) -> bool:
    return order.side == OrderSide.SELL and order.open


def _open_order_shares(order) -> Decimal:
    if order.remaining_shares is not None:
        return max(order.remaining_shares, Decimal("0"))
    if order.size_shares is not None:
        return max(order.size_shares, Decimal("0"))
    return Decimal("0")


def _abnormal_live_state_pause_reason(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
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


def _live_state_age_seconds(context: ExtensionContext, observed_at: datetime) -> float:
    current_time = context.now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return (current_time.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()


def _max_live_state_age_seconds(config: CurrentStrategyConfig, game) -> int:
    """按运动项目选择恢复侧的新鲜度窗口。"""

    league = str(getattr(game, "league", "") or "").strip().lower()
    if getattr(game, "tennis_state", None) is not None or "tennis" in league or league in {"atp", "wta"}:
        return config.sports_tennis_max_game_state_age_seconds
    return config.sports_max_game_state_age_seconds


def _recovery_metadata(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    abnormal_pause_reason: str | None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "sports_recovery_reason": abnormal_pause_reason or "strategy_recovery",
    }
    if abnormal_pause_reason is not None:
        metadata["sports_recovery_pause_reason"] = abnormal_pause_reason
        metadata.update(
            build_exit_plan_metadata(
                config,
                context,
                token_id=context.token_id,
                source_reason=abnormal_pause_reason,
            )
        )
    return metadata
