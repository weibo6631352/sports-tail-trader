"""当前策略的恢复与修复语义。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.domain.order import OrderSide
from polymarket_trader.extension_api import RecoveryDecision, ExtensionContext, ExtensionDecision

from strategies.current.config import CurrentStrategyConfig
from strategies.current.outcomes import sports_token_targets


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
            actions.append(
                ExtensionDecision.sell(
                    reason="recovery_exit_shortage",
                    token_id=position.token_id,
                    price=config.exit_no_price,
                    size_shares=uncovered_shares,
                    market_slug=position.market_slug or context.market.market_slug,
                )
            )

    pause_trading = context.market.trading_status in {
        TradingStatus.PAUSED,
        TradingStatus.CLOSED,
        TradingStatus.RESOLVED,
    } or (
        account_snapshot is not None and account_snapshot.is_market_paused(context.market.condition_id)
    )
    return RecoveryDecision(
        reason="strategy_recovery",
        actions=tuple(actions),
        pause_trading=pause_trading,
        pause_reason="market_not_tradable" if pause_trading else "",
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
