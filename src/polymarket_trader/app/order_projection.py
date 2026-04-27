from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    Order,
    OrderRecord,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    SellOrderIntent,
    TradableOrderIntent,
)
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.account import AccountSnapshot, MarketPauseSource
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.serialization import utc_now


def order_result_to_order_status(result: OrderResult) -> OrderStatus:
    return {
        OrderResultStatus.FULL_FILL: OrderStatus.MATCHED,
        OrderResultStatus.PARTIAL_FILL: OrderStatus.PARTIALLY_FILLED,
        OrderResultStatus.NO_FILL: OrderStatus.NO_FILL,
        OrderResultStatus.LIVE: OrderStatus.LIVE,
        OrderResultStatus.REJECTED: OrderStatus.REJECTED,
        OrderResultStatus.FAILED: OrderStatus.FAILED,
        OrderResultStatus.CANCELLED: OrderStatus.CANCELLED,
        OrderResultStatus.UNKNOWN_TIMEOUT: OrderStatus.FAILED,
    }.get(result.status, OrderStatus.FAILED)


def order_status_to_text(status: OrderResultStatus) -> str:
    return {
        OrderResultStatus.FULL_FILL: "full_fill",
        OrderResultStatus.PARTIAL_FILL: "partial_fill",
        OrderResultStatus.NO_FILL: "no_fill",
        OrderResultStatus.LIVE: "live",
        OrderResultStatus.REJECTED: "rejected",
        OrderResultStatus.FAILED: "failed",
        OrderResultStatus.CANCELLED: "cancelled",
        OrderResultStatus.UNKNOWN_TIMEOUT: "unknown_timeout",
    }.get(status, "unknown")


def normalize_order_id(order: Order) -> str:
    return order.order_id or order.idempotency_key or (
        f"{order.condition_id}:{order.token_id}:{order.side.value}:{order.status.value}"
    )


def order_open_size(order: Order) -> Decimal:
    if order.remaining_shares is not None:
        return max(order.remaining_shares, Decimal("0"))
    if order.size_shares is not None:
        return max(order.size_shares, Decimal("0"))
    if order.amount_usdc is not None:
        return max(order.amount_usdc, Decimal("0"))
    return Decimal("0")


def order_open_shares(order: Order) -> Decimal | None:
    if order.remaining_shares is not None:
        return max(order.remaining_shares, Decimal("0"))
    if order.size_shares is not None:
        return max(order.size_shares, Decimal("0"))
    return None


def filled_shares(result: OrderResult) -> Decimal:
    if result.matched_shares > Decimal("0"):
        return result.matched_shares
    if result.requested_size_shares is not None:
        return result.requested_size_shares - result.remaining_shares
    if result.notional_usdc > Decimal("0") and result.price and result.price > Decimal("0"):
        return result.notional_usdc / result.price
    return Decimal("0")


def spent_usdc(result: OrderResult) -> Decimal:
    if result.spent_usdc > Decimal("0"):
        return result.spent_usdc
    if result.price is not None and result.matched_shares > Decimal("0"):
        return result.price * result.matched_shares
    return Decimal("0")


def released_budget(result: OrderResult) -> Decimal:
    requested = result.requested_amount_usdc or result.notional_usdc
    released = requested - result.spent_usdc
    if released < Decimal("0"):
        return Decimal("0")
    return released


def has_unexpected_resting_order(result: OrderResult) -> bool:
    return result.order_type == OrderType.FAK and (
        result.has_resting_order or result.status == OrderResultStatus.LIVE
    )


def find_open_order(
    snapshot: AccountSnapshot | None,
    condition_id: str,
    token_id: str,
    order_id: str | None,
) -> Order | None:
    if snapshot is None or order_id is None:
        return None
    for order in snapshot.open_orders_for_market(condition_id, token_id):
        candidate_id = order.order_id or order.idempotency_key
        if candidate_id == order_id:
            return order
    return None


@dataclass(slots=True)
class AccountStateProjector:
    store: AccountStateStore

    def apply_buy_result(self, result: OrderResult, *, snapshot: AccountSnapshot | None) -> None:
        if result.status not in {
            OrderResultStatus.FULL_FILL,
            OrderResultStatus.PARTIAL_FILL,
            OrderResultStatus.NO_FILL,
        }:
            return
        position = _snapshot_position(snapshot, result.condition_id, result.token_id)
        fill_size = filled_shares(result)
        cost = spent_usdc(result)
        if position is None and fill_size <= Decimal("0"):
            return
        if position is None:
            position = Position(
                condition_id=result.condition_id,
                token_id=result.token_id,
                shares=fill_size,
                cost_usdc=cost,
                market_slug=result.market_slug,
                open_sell_shares=Decimal("0"),
                pending_buy_shares=Decimal("0"),
                confirmation_status=str(result.status),
                last_order_id=result.order_id,
                last_trade_id=result.trade_id,
                updated_at=utc_now(),
            )
        else:
            position = Position(
                condition_id=position.condition_id,
                token_id=position.token_id,
                shares=position.shares + fill_size,
                cost_usdc=position.cost_usdc + cost,
                market_slug=position.market_slug or result.market_slug,
                open_buy_shares=Decimal("0"),
                open_sell_shares=position.open_sell_shares,
                pending_buy_shares=Decimal("0"),
                confirmed_shares=position.confirmed_shares + fill_size,
                last_order_id=result.order_id or position.last_order_id,
                last_trade_id=result.trade_id or position.last_trade_id,
                confirmation_status=str(result.status),
                updated_at=utc_now(),
            )
        self.store.upsert_position(position)
        self.refresh_balances_from(snapshot)

    def apply_sell_result(
        self,
        result: OrderResult,
        *,
        snapshot: AccountSnapshot | None,
        intent: SellOrderIntent,
    ) -> None:
        if result.status not in {
            OrderResultStatus.FULL_FILL,
            OrderResultStatus.PARTIAL_FILL,
            OrderResultStatus.LIVE,
        }:
            return
        current_snapshot = self.store.snapshot()
        position = _snapshot_position(current_snapshot, result.condition_id, result.token_id)
        if position is None:
            position = _snapshot_position(snapshot, result.condition_id, result.token_id)
        if position is None:
            return

        remaining_shares = result.remaining_shares
        if (
            result.status in {OrderResultStatus.LIVE, OrderResultStatus.PARTIAL_FILL}
            and remaining_shares <= Decimal("0")
        ):
            remaining_shares = intent.size_shares - filled_shares(result)
        if remaining_shares < Decimal("0"):
            remaining_shares = Decimal("0")

        open_sell = max(position.open_sell_shares - _existing_order_size(current_snapshot, result), Decimal("0"))
        open_sell += remaining_shares
        if open_sell < Decimal("0"):
            open_sell = Decimal("0")

        order_id = result.order_id or intent.idempotency_key or (
            f"{result.trace_id}:{result.condition_id}:{result.token_id}:sell"
        )
        if result.status in {OrderResultStatus.LIVE, OrderResultStatus.PARTIAL_FILL}:
            self.store.upsert_order(
                Order(
                    trace_id=result.trace_id,
                    condition_id=result.condition_id,
                    token_id=result.token_id,
                    market_slug=position.market_slug or result.market_slug,
                    side=OrderSide.SELL,
                    order_type=result.order_type or intent.order_type,
                    price=result.price or intent.price,
                    size_shares=result.requested_size_shares or intent.size_shares,
                    filled_shares=result.matched_shares,
                    remaining_shares=remaining_shares,
                    notional_usdc=result.notional_usdc,
                    order_id=order_id,
                    trade_id=result.trade_id,
                    status=order_result_to_order_status(result),
                    idempotency_key=intent.idempotency_key or order_id,
                    reason=result.reason,
                    post_only=intent.post_only,
                    created_at=result.timestamps.queued_at,
                    updated_at=result.timestamps.ack_at,
                )
            )
        else:
            self.store.remove_order(order_id)

        self.store.upsert_position(
            Position(
                condition_id=position.condition_id,
                token_id=position.token_id,
                shares=position.shares,
                cost_usdc=position.cost_usdc,
                market_slug=position.market_slug or result.market_slug,
                open_buy_shares=position.open_buy_shares,
                open_sell_shares=open_sell,
                pending_buy_shares=position.pending_buy_shares,
                confirmed_shares=position.confirmed_shares,
                last_order_id=result.order_id or position.last_order_id,
                last_trade_id=result.trade_id or position.last_trade_id,
                confirmation_status=str(result.status),
                updated_at=utc_now(),
            )
        )

    def apply_result_flags(self, result: OrderResult, *, snapshot: AccountSnapshot | None) -> None:
        if has_unexpected_resting_order(result):
            self.store.set_allow_new_entries(False)
            self.store.pause_market(
                result.condition_id,
                reason="unexpected_resting_order",
                source=MarketPauseSource.RISK,
            )
        if result.status in {
            OrderResultStatus.REJECTED,
            OrderResultStatus.FAILED,
            OrderResultStatus.UNKNOWN_TIMEOUT,
        }:
            self.store.set_allow_new_entries(True)
        self.refresh_balances_from(snapshot)

    def apply_submitted_intent(
        self,
        intent: TradableOrderIntent,
        *,
        market_slug: str | None,
        snapshot: AccountSnapshot,
        reason: str,
    ) -> None:
        order_id = intent.idempotency_key or (
            f"{intent.trace_id}:{intent.condition_id}:{intent.token_id}:{intent.side.value.lower()}"
        )
        buy_open_shares = (
            (intent.amount_usdc / intent.price)
            if isinstance(intent, BuyOrderIntent) and intent.price > 0
            else Decimal("0")
        )
        current_position = snapshot.get_position(intent.condition_id, intent.token_id)
        if current_position is None:
            current_position = Position(
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                shares=Decimal("0"),
                cost_usdc=Decimal("0"),
                market_slug=market_slug,
                open_buy_shares=buy_open_shares,
                open_sell_shares=intent.size_shares if isinstance(intent, SellOrderIntent) else Decimal("0"),
                pending_buy_shares=buy_open_shares,
            )
        elif isinstance(intent, SellOrderIntent):
            current_position = current_position.with_open_sell_shares(
                snapshot.open_sell_shares_for_market(intent.condition_id, intent.token_id) + intent.size_shares
            )
        else:
            open_buy_shares = current_position.open_buy_shares + buy_open_shares
            current_position = current_position.with_open_buy_shares(open_buy_shares)
            current_position = current_position.with_pending_buy_shares(
                current_position.pending_buy_shares + buy_open_shares
            )

        self.store.upsert_order(
            OrderRecord(
                trace_id=intent.trace_id,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                side=intent.side,
                order_type=intent.order_type,
                price=intent.price,
                market_slug=intent.market_slug,
                size_shares=getattr(intent, "size_shares", None),
                remaining_shares=getattr(intent, "size_shares", None),
                amount_usdc=getattr(intent, "amount_usdc", None),
                notional_usdc=intent.notional_usdc,
                order_id=order_id,
                status=OrderStatus.SUBMITTED,
                idempotency_key=intent.idempotency_key or order_id,
                reason=reason,
                post_only=intent.post_only,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        self.store.upsert_position(current_position)

    def apply_replace_result(
        self,
        market: Market,
        *,
        source_order: Order,
        result: OrderResult,
        operator: str,
        reason: str,
    ) -> None:
        self.store.remove_order(normalize_order_id(source_order))
        order_status = order_result_to_order_status(result)
        replacement_size_shares = result.requested_size_shares or order_open_shares(source_order) or source_order.size_shares
        replacement_remaining = _replacement_remaining(result, replacement_size_shares)

        if order_status in {
            OrderStatus.CREATED,
            OrderStatus.SIGNED,
            OrderStatus.SUBMITTED,
            OrderStatus.LIVE,
            OrderStatus.MATCHED,
            OrderStatus.PARTIALLY_FILLED,
        }:
            order_id = result.order_id or result.trace_id
            replacement_side = result.side or source_order.side
            replacement_order_type = result.order_type or source_order.order_type
            replacement_price = result.price or source_order.price
            amount_usdc = result.requested_amount_usdc
            if amount_usdc is None and replacement_side == OrderSide.BUY:
                amount_usdc = source_order.amount_usdc
            notional_usdc = result.notional_usdc
            if (
                notional_usdc == Decimal("0")
                and replacement_size_shares is not None
                and replacement_price is not None
            ):
                notional_usdc = replacement_price * replacement_size_shares
            self.store.upsert_order(
                Order(
                    trace_id=result.trace_id,
                    condition_id=result.condition_id,
                    token_id=result.token_id,
                    market_slug=source_order.market_slug or market.market_slug,
                    side=replacement_side,
                    order_type=replacement_order_type,
                    price=replacement_price,
                    amount_usdc=amount_usdc,
                    size_shares=replacement_size_shares,
                    filled_shares=result.matched_shares,
                    remaining_shares=replacement_remaining,
                    notional_usdc=notional_usdc,
                    order_id=order_id,
                    trade_id=result.trade_id,
                    status=order_status,
                    idempotency_key=result.intent.idempotency_key if result.intent is not None else order_id,
                    reason=f"{reason}:{operator}",
                    post_only=bool(getattr(result.intent, "post_only", False)),
                    created_at=result.timestamps.queued_at,
                    updated_at=result.timestamps.ack_at,
                )
            )

        self.refresh_position_coverage(
            condition_id=source_order.condition_id,
            token_id=source_order.token_id,
            market_slug=source_order.market_slug or market.market_slug,
            last_order_id=result.order_id or result.trace_id,
            last_trade_id=result.trade_id,
            confirmation_status=order_status_to_text(result.status),
            updated_at=result.timestamps.ack_at,
        )

    def refresh_position_coverage(
        self,
        *,
        condition_id: str,
        token_id: str,
        market_slug: str | None,
        last_order_id: str | None,
        last_trade_id: str | None,
        confirmation_status: str,
        updated_at: datetime | None,
    ) -> None:
        snapshot = self.store.snapshot()
        position = snapshot.get_position(condition_id, token_id)
        open_buy_shares = sum(
            order_open_shares(order) or Decimal("0")
            for order in snapshot.open_buy_orders_for_market(condition_id, token_id)
        ) or Decimal("0")
        open_sell_shares = snapshot.open_sell_shares_for_market(condition_id, token_id)
        pending_buy_shares = Decimal(open_buy_shares)
        if position is None and open_buy_shares <= Decimal("0") and open_sell_shares <= Decimal("0"):
            return
        current_position = position or Position(
            condition_id=condition_id,
            token_id=token_id,
            shares=Decimal("0"),
            cost_usdc=Decimal("0"),
            market_slug=market_slug,
        )
        self.store.upsert_position(
            Position(
                condition_id=current_position.condition_id,
                token_id=current_position.token_id,
                shares=current_position.shares,
                cost_usdc=current_position.cost_usdc,
                market_slug=current_position.market_slug or market_slug,
                open_buy_shares=open_buy_shares,
                open_sell_shares=open_sell_shares,
                pending_buy_shares=pending_buy_shares,
                confirmed_shares=current_position.confirmed_shares,
                last_order_id=last_order_id,
                last_trade_id=last_trade_id,
                confirmation_status=confirmation_status,
                updated_at=updated_at,
            )
        )

    def refresh_balances_from(self, snapshot: AccountSnapshot | None) -> None:
        if snapshot is None:
            return
        self.store.update_balances(
            balance_usdc=snapshot.balance_usdc,
            allowance_usdc=snapshot.allowance_usdc,
        )


def _snapshot_position(
    snapshot: AccountSnapshot | None,
    condition_id: str,
    token_id: str,
) -> Position | None:
    if snapshot is None:
        return None
    return snapshot.get_position(condition_id, token_id)


def _existing_order_size(snapshot: AccountSnapshot | None, result: OrderResult) -> Decimal:
    if snapshot is None:
        return Decimal("0")
    for order in snapshot.open_orders_for_market(result.condition_id, result.token_id):
        if order.order_id == result.order_id or (
            result.order_id is None and order.idempotency_key == getattr(result.intent, "idempotency_key", None)
        ):
            return order.remaining_shares or order.size_shares or Decimal("0")
    return Decimal("0")


def _replacement_remaining(
    result: OrderResult,
    replacement_size_shares: Decimal | None,
) -> Decimal:
    replacement_remaining = result.remaining_shares
    if (
        result.status in {OrderResultStatus.LIVE, OrderResultStatus.PARTIAL_FILL}
        and replacement_remaining <= Decimal("0")
        and replacement_size_shares is not None
    ):
        replacement_remaining = max(replacement_size_shares - result.matched_shares, Decimal("0"))
    if replacement_remaining < Decimal("0"):
        replacement_remaining = Decimal("0")
    return replacement_remaining
