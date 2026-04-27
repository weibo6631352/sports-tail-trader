"""当前策略的恢复与修复语义。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.domain.order import OrderSide
from polymarket_trader.extension_api import RecoveryDecision, ExtensionContext, ExtensionDecision

from strategies.current.config import CurrentStrategyConfig
from strategies.current.outcomes import primary_token_id


def decide_recovery(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> RecoveryDecision:
    if context.market is None:
        return RecoveryDecision(reason="missing_market_state")

    try:
        managed_token_id = primary_token_id(context.market)
    except ValueError:
        return RecoveryDecision(
            reason="missing_primary_outcome",
            pause_trading=True,
            pause_reason="missing_primary_outcome",
        )

    account_snapshot = context.account_snapshot
    position = context.position if context.position is not None and context.position.token_id == managed_token_id else None
    if position is None and account_snapshot is not None:
        position = account_snapshot.get_position(
            context.market.condition_id,
            managed_token_id,
        )

    open_orders = tuple(order for order in context.open_orders if order.token_id == managed_token_id)
    if not open_orders and account_snapshot is not None:
        open_orders = account_snapshot.open_orders_for_market(
            context.market.condition_id,
            managed_token_id,
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

    open_exit_shares = sum(
        (
            _open_order_shares(order)
            for order in open_orders
            if _is_open_exit_order(order)
        ),
        start=Decimal("0"),
    )
    if position is not None:
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
