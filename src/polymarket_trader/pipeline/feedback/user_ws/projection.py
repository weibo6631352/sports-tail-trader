from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEvent, DomainEventType, Fill
from polymarket_trader.domain.order import Order, OrderStatus
from polymarket_trader.domain.position import Position
from polymarket_trader.infra.polymarket import user_ws_adapter
from polymarket_trader.runtime.account_state import AccountStateStore

flatten_message = user_ws_adapter.flatten_message
message_type = user_ws_adapter.message_type
extract_trace_id = user_ws_adapter.extract_trace_id
first_value = user_ws_adapter.first_value
coalesce_decimal = user_ws_adapter.coalesce_decimal
extract_condition_id = user_ws_adapter.extract_condition_id
extract_token_id = user_ws_adapter.extract_token_id
extract_market_slug = user_ws_adapter.extract_market_slug
iter_order_snapshots = user_ws_adapter.iter_order_snapshots
iter_position_snapshots = user_ws_adapter.iter_position_snapshots
iter_fill_snapshots = user_ws_adapter.iter_fill_snapshots
is_snapshot_message = user_ws_adapter.is_snapshot_message
order_from_fill = user_ws_adapter.order_from_fill
apply_fill_to_position = user_ws_adapter.apply_fill_to_position


class UserWsAccountProjector:
    def __init__(self, account_state: AccountStateStore) -> None:
        self._account_state = account_state

    async def handle_balance(
        self,
        payload: Mapping[str, Any],
        *,
        trace_id: str,
    ) -> list[DomainEvent]:
        snapshot_before = self._account_state.snapshot()
        balance_usdc = coalesce_decimal(
            first_value(payload, "balance_usdc", "available_balance_usdc", "usdc_balance", "balance"),
            first_value(payload, "available_usdc", "available_balance", "available"),
            default=snapshot_before.balance_usdc,
        )
        allowance_usdc = coalesce_decimal(
            first_value(payload, "allowance_usdc", "allowance"),
            first_value(payload, "approved_usdc", "approval_usdc"),
            default=snapshot_before.allowance_usdc,
        )
        snapshot = self._account_state.update_balances(
            balance_usdc=balance_usdc,
            allowance_usdc=allowance_usdc,
        )
        return [
            build_event(
                DomainEventType.BALANCE_UPDATED,
                trace_id=trace_id,
                condition_id=extract_condition_id(payload),
                token_id=extract_token_id(payload),
                market_slug=extract_market_slug(payload),
                reason="balance_update",
                payload={
                    "balance_usdc": str(snapshot.balance_usdc),
                    "allowance_usdc": str(snapshot.allowance_usdc),
                    "user_ws_connected": snapshot.user_ws_connected,
                    "allow_new_entries": snapshot.allow_new_entries,
                    "market_pauses": [pause.as_payload() for pause in snapshot.market_pauses],
                    "last_reconcile_at": (
                        None
                        if snapshot.last_reconcile_at is None
                        else snapshot.last_reconcile_at.isoformat()
                    ),
                },
            )
        ]

    async def handle_position(
        self,
        payload: Mapping[str, Any],
        *,
        trace_id: str,
    ) -> list[DomainEvent]:
        positions = tuple(iter_position_snapshots(payload))
        if not positions:
            return []
        if is_snapshot_message(payload):
            snapshot = self._account_state.replace_positions(positions)
        else:
            snapshot = self._account_state.snapshot()
            for position in positions:
                snapshot = self._account_state.upsert_position(position)
        return [
            build_event(
                DomainEventType.POSITION_UPDATED,
                trace_id=trace_id,
                condition_id=extract_condition_id(payload),
                token_id=extract_token_id(payload),
                market_slug=extract_market_slug(payload),
                reason="position_update",
                payload={
                    "positions": [position_to_payload(position) for position in positions],
                    "position_count": len(positions),
                    "snapshot": snapshot_to_payload(snapshot),
                },
            )
        ]

    async def handle_order(
        self,
        payload: Mapping[str, Any],
        *,
        trace_id: str,
    ) -> list[DomainEvent]:
        orders = tuple(iter_order_snapshots(payload))
        if not orders:
            return []
        snapshot = self._account_state.snapshot()
        emitted: list[DomainEvent] = []
        for order in orders:
            if order.status in {
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.FAILED,
                OrderStatus.NO_FILL,
            }:
                snapshot = self._account_state.remove_order(order_identity(order))
            else:
                snapshot = self._account_state.upsert_order(order)
            emitted.append(
                build_event(
                    DomainEventType.ORDER_STATE_UPDATED,
                    trace_id=trace_id,
                    condition_id=order.condition_id,
                    token_id=order.token_id,
                    market_slug=order.market_slug,
                    reason=order.reason or "order_update",
                    payload={
                        "order": order_to_payload(order),
                        "open_orders": [
                            order_to_payload(open_order)
                            for open_order in snapshot.open_orders_for_market(
                                order.condition_id,
                                order.token_id,
                            )
                        ],
                        "snapshot": snapshot_to_payload(snapshot),
                    },
                )
            )
        return emitted

    async def handle_fill(
        self,
        payload: Mapping[str, Any],
        *,
        trace_id: str,
    ) -> list[DomainEvent]:
        fills = tuple(iter_fill_snapshots(payload))
        if not fills:
            return []

        snapshot = self._account_state.snapshot()
        emitted: list[DomainEvent] = []
        for fill in fills:
            snapshot = self._account_state.record_fill(fill)
            position = apply_fill_to_position(
                snapshot.get_position(fill.condition_id or "", fill.token_id or ""),
                fill,
            )
            if position is not None:
                snapshot = self._account_state.upsert_position(position)
            if fill.order_id is not None:
                order = order_from_fill(fill)
                if order.status in {
                    OrderStatus.CANCELLED,
                    OrderStatus.REJECTED,
                    OrderStatus.FAILED,
                    OrderStatus.NO_FILL,
                }:
                    snapshot = self._account_state.remove_order(order_identity(order))
                else:
                    snapshot = self._account_state.upsert_order(order)
                emitted.append(
                    build_event(
                        DomainEventType.ORDER_STATE_UPDATED,
                        trace_id=trace_id,
                        condition_id=fill.condition_id,
                        token_id=fill.token_id,
                        market_slug=fill.market_slug,
                        reason=fill.reason or "trade_update",
                        payload={
                            "order": order_to_payload(order),
                            "fill": fill_to_payload(fill),
                            "position_projected": position is not None,
                            "account_projected": True,
                            "snapshot": snapshot_to_payload(snapshot),
                        },
                    )
                )

            emitted.append(
                build_event(
                    DomainEventType.FILL_RECORDED,
                    trace_id=trace_id,
                    condition_id=fill.condition_id,
                    token_id=fill.token_id,
                    market_slug=fill.market_slug,
                    reason=fill.reason or fill.status,
                    payload={
                        "fill": fill_to_payload(fill),
                        "position": None if position is None else position_to_payload(position),
                        # 同一笔成交已通过上面的 ORDER_STATE_UPDATED 进入交易决策；
                        # 成交审计事件只落库复盘，避免重复触发跟单 SELL。
                        "skip_trading_decision_order_result": True,
                        "position_projected": position is not None,
                        "account_projected": True,
                        "snapshot": snapshot_to_payload(snapshot),
                    },
                )
            )
            if position is not None:
                emitted.append(
                    build_event(
                        DomainEventType.POSITION_UPDATED,
                        trace_id=trace_id,
                        condition_id=fill.condition_id,
                        token_id=fill.token_id,
                        market_slug=fill.market_slug,
                        reason=fill.reason or "position_after_fill",
                        payload={
                            "position": position_to_payload(position),
                            "fill": fill_to_payload(fill),
                            "snapshot": snapshot_to_payload(snapshot),
                        },
                    )
                )
        return emitted


def order_identity(order: Order) -> str:
    return order.order_id or order.idempotency_key or (
        f"{order.condition_id}:{order.token_id}:{order.side.value}:{order.order_type.value}"
    )


def order_to_payload(order: Order) -> dict[str, Any]:
    return {
        "trace_id": order.trace_id,
        "condition_id": order.condition_id,
        "token_id": order.token_id,
        "market_slug": order.market_slug,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "price": str(order.price),
        "amount_usdc": None if order.amount_usdc is None else str(order.amount_usdc),
        "size_shares": None if order.size_shares is None else str(order.size_shares),
        "notional_usdc": None if order.notional_usdc is None else str(order.notional_usdc),
        "order_id": order.order_id,
        "trade_id": order.trade_id,
        "status": order.status.value,
        "idempotency_key": order.idempotency_key,
        "reason": order.reason,
        "post_only": order.post_only,
    }


def position_to_payload(position: Position) -> dict[str, Any]:
    return {
        "condition_id": position.condition_id,
        "token_id": position.token_id,
        "market_slug": position.market_slug,
        "shares": str(position.shares),
        "cost_usdc": str(position.cost_usdc),
        "open_buy_shares": str(position.open_buy_shares),
        "open_sell_shares": str(position.open_sell_shares),
        "pending_buy_shares": str(position.pending_buy_shares),
        "confirmed_shares": str(position.confirmed_shares),
        "last_order_id": position.last_order_id,
        "last_trade_id": position.last_trade_id,
        "confirmation_status": position.confirmation_status,
    }


def fill_to_payload(fill: Fill) -> dict[str, Any]:
    return {
        "trace_id": fill.trace_id,
        "event_id": fill.event_id,
        "condition_id": fill.condition_id,
        "token_id": fill.token_id,
        "market_slug": fill.market_slug,
        "order_id": fill.order_id,
        "trade_id": fill.trade_id,
        "side": fill.side,
        "price": None if fill.price is None else str(fill.price),
        "size": None if fill.size is None else str(fill.size),
        "notional_usdc": None if fill.notional_usdc is None else str(fill.notional_usdc),
        "status": fill.status,
        "reason": fill.reason,
        "confirmed_at": None if fill.confirmed_at is None else fill.confirmed_at.isoformat(),
    }


def snapshot_to_payload(snapshot: AccountSnapshot) -> dict[str, Any]:
    return {
        "balance_usdc": str(snapshot.balance_usdc),
        "allowance_usdc": str(snapshot.allowance_usdc),
        "user_ws_connected": snapshot.user_ws_connected,
        "allow_new_entries": snapshot.allow_new_entries,
        "market_pauses": tuple(pause.as_payload() for pause in snapshot.market_pauses),
        "last_reconcile_at": (
            None if snapshot.last_reconcile_at is None else snapshot.last_reconcile_at.isoformat()
        ),
        "positions": [position_to_payload(position) for position in snapshot.positions],
        "open_orders": [order_to_payload(order) for order in snapshot.open_orders],
        "fills": [fill_to_payload(fill) for fill in snapshot.fills],
    }


def build_event(
    event_type: DomainEventType,
    *,
    trace_id: str,
    condition_id: str | None = None,
    token_id: str | None = None,
    market_slug: str | None = None,
    reason: str = "",
    payload: Mapping[str, Any] | None = None,
) -> DomainEvent:
    return DomainEvent(
        trace_id=trace_id,
        event_type=event_type,
        event_id=uuid4().hex,
        condition_id=condition_id,
        token_id=token_id,
        market_slug=market_slug,
        reason=reason,
        created_at=datetime.now(timezone.utc),
        payload=dict(payload or {}),
    )
