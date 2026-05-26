from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from polymarket_trader.app.decision_serialization import (
    TRADING_DECISION_WORKER_ORIGIN as TRADING_DECISION_WORKER_ORIGIN,
    serialize_allocation as serialize_allocation,
    serialize_allocation_plan as serialize_allocation_plan,
    serialize_control_intent as serialize_control_intent,
    serialize_intent as serialize_intent,
    serialize_order_result as serialize_order_result,
    serialize_plan_metadata as serialize_plan_metadata,
    serialize_review as serialize_review,
    serialize_snapshot as serialize_snapshot,
    snapshot_allowance as snapshot_allowance,
    snapshot_available_usdc as snapshot_available_usdc,
    snapshot_balance as snapshot_balance,
    snapshot_position as snapshot_position,
)
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.order import (
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderType,
)


def is_self_emitted(event: DomainEvent) -> bool:
    return event.payload.get("origin") == TRADING_DECISION_WORKER_ORIGIN


def coerce_order_result_from_event(event: DomainEvent) -> OrderResult | None:
    payload = dict(event.payload)
    if payload.get("skip_trading_decision_order_result"):
        return None

    order_result_payload = payload.get("order_result")
    if isinstance(order_result_payload, Mapping):
        payload = {**payload, **dict(order_result_payload)}
    else:
        payload = _merge_nested_order_payload(payload)

    status_value = payload.get("status") or payload.get("order_status")
    if status_value is None and event.event_type not in {
        DomainEventType.ORDER_MATCHED,
        DomainEventType.ORDER_PARTIALLY_FILLED,
        DomainEventType.ORDER_NO_FILL,
        DomainEventType.ORDER_REJECTED,
        DomainEventType.ORDER_CANCELLED,
        DomainEventType.ORDER_STATE_UPDATED,
        DomainEventType.FILL_RECORDED,
        DomainEventType.FOLLOW_UP_ORDER_SUBMITTED,
    }:
        return None
    status = coerce_status(status_value, event.event_type)
    return OrderResult(
        trace_id=str(payload.get("trace_id", event.trace_id)),
        condition_id=str(payload.get("condition_id", event.condition_id or "")),
        token_id=str(payload.get("token_id", event.token_id or "")),
        status=status,
        market_slug=payload.get("market_slug", event.market_slug),
        order_id=payload.get("order_id"),
        trade_id=payload.get("trade_id"),
        side=coerce_side(payload.get("side")),
        order_type=coerce_order_type(payload.get("order_type")),
        price=decimal_or_none(payload.get("price")),
        requested_amount_usdc=decimal_or_none(
            payload.get("requested_amount_usdc") or payload.get("amount_usdc")
        ),
        requested_size_shares=decimal_or_none(
            payload.get("requested_size_shares") or payload.get("size_shares")
        ),
        matched_shares=decimal_or_none(
            payload.get("matched_shares")
            or payload.get("filled_shares")
            or payload.get("fill_size")
        ) or Decimal("0"),
        remaining_shares=decimal_or_none(payload.get("remaining_shares")) or Decimal("0"),
        spent_usdc=decimal_or_none(payload.get("spent_usdc") or payload.get("fill_notional_usdc")) or Decimal("0"),
        notional_usdc=decimal_or_none(
            payload.get("notional_usdc")
            or payload.get("fill_notional_usdc")
        ) or Decimal("0"),
        reason=str(payload.get("reason", event.reason or "")),
        retryable=bool(payload.get("retryable", False)),
        raw_response_summary=payload.get("raw_response_summary"),
    )


def _merge_nested_order_payload(payload: dict[str, object]) -> dict[str, object]:
    """把用户 WS 的 nested order/fill 事件规范成 OrderResult 字段。

    用户 WS 投影事件通常把订单和成交分别放在 ``order`` / ``fill`` 中；
    交易 worker 只消费框架内部 ``OrderResult`` 语义，所以这里在 worker
    边界做一次 DTO 规整，避免策略或 app 层理解外部 payload 结构。
    """

    order_payload = payload.get("order")
    fill_payload = payload.get("fill")
    merged = dict(payload)
    if isinstance(order_payload, Mapping):
        order = dict(order_payload)
        merged.update(
            {
                "trace_id": order.get("trace_id") or merged.get("trace_id"),
                "condition_id": order.get("condition_id") or merged.get("condition_id"),
                "token_id": order.get("token_id") or merged.get("token_id"),
                "market_slug": order.get("market_slug") or merged.get("market_slug"),
                "side": order.get("side") or merged.get("side"),
                "order_type": order.get("order_type") or merged.get("order_type"),
                "price": order.get("price") or merged.get("price"),
                "amount_usdc": order.get("amount_usdc") or merged.get("amount_usdc"),
                "size_shares": order.get("size_shares") or merged.get("size_shares"),
                "filled_shares": order.get("filled_shares") or merged.get("filled_shares"),
                "remaining_shares": order.get("remaining_shares") or merged.get("remaining_shares"),
                "notional_usdc": order.get("notional_usdc") or merged.get("notional_usdc"),
                "order_id": order.get("order_id") or merged.get("order_id"),
                "trade_id": order.get("trade_id") or merged.get("trade_id"),
                "status": order.get("status") or merged.get("status"),
                "idempotency_key": order.get("idempotency_key") or merged.get("idempotency_key"),
                "reason": order.get("reason") or merged.get("reason"),
            }
        )
    if isinstance(fill_payload, Mapping):
        fill = dict(fill_payload)
        merged.update(
            {
                "trace_id": fill.get("trace_id") or merged.get("trace_id"),
                "condition_id": fill.get("condition_id") or merged.get("condition_id"),
                "token_id": fill.get("token_id") or merged.get("token_id"),
                "market_slug": fill.get("market_slug") or merged.get("market_slug"),
                "side": fill.get("side") or merged.get("side"),
                "price": fill.get("price") or merged.get("price"),
                "fill_size": fill.get("size") or fill.get("filled_size") or merged.get("fill_size"),
                "fill_notional_usdc": fill.get("notional_usdc") or fill.get("amount") or merged.get("fill_notional_usdc"),
                "order_id": fill.get("order_id") or merged.get("order_id"),
                "trade_id": fill.get("trade_id") or merged.get("trade_id"),
                "status": fill.get("status") or merged.get("status"),
                "reason": fill.get("reason") or merged.get("reason"),
            }
        )
    return merged


def coerce_status(
    status_value: object | None,
    event_type: DomainEventType | str,
) -> OrderResultStatus:
    if isinstance(status_value, OrderResultStatus):
        return status_value
    if isinstance(status_value, str):
        text = status_value.strip().lower().replace("-", "_").replace(" ", "_")
        mapping = {
            "full_fill": OrderResultStatus.FULL_FILL,
            "full": OrderResultStatus.FULL_FILL,
            "filled": OrderResultStatus.FULL_FILL,
            "matched": OrderResultStatus.FULL_FILL,
            "match": OrderResultStatus.FULL_FILL,
            "confirmed": OrderResultStatus.FULL_FILL,
            "mined": OrderResultStatus.FULL_FILL,
            "partial_fill": OrderResultStatus.PARTIAL_FILL,
            "partial": OrderResultStatus.PARTIAL_FILL,
            "partially_filled": OrderResultStatus.PARTIAL_FILL,
            "no_fill": OrderResultStatus.NO_FILL,
            "nofill": OrderResultStatus.NO_FILL,
            "live": OrderResultStatus.LIVE,
            "resting": OrderResultStatus.LIVE,
            "open_live": OrderResultStatus.LIVE,
            "submitted": OrderResultStatus.LIVE,
            "signed": OrderResultStatus.LIVE,
            "created": OrderResultStatus.LIVE,
            "cancelled": OrderResultStatus.CANCELLED,
            "canceled": OrderResultStatus.CANCELLED,
            "rejected": OrderResultStatus.REJECTED,
            "failed": OrderResultStatus.FAILED,
            "error": OrderResultStatus.FAILED,
            "unknown_timeout": OrderResultStatus.UNKNOWN_TIMEOUT,
            "timeout": OrderResultStatus.UNKNOWN_TIMEOUT,
        }
        if text in mapping:
            return mapping[text]
        try:
            return OrderResultStatus(status_value)
        except ValueError:
            pass
    event_name = str(event_type)
    if event_name == DomainEventType.ORDER_MATCHED.value:
        return OrderResultStatus.FULL_FILL
    if event_name == DomainEventType.ORDER_PARTIALLY_FILLED.value:
        return OrderResultStatus.PARTIAL_FILL
    if event_name == DomainEventType.ORDER_NO_FILL.value:
        return OrderResultStatus.NO_FILL
    if event_name == DomainEventType.ORDER_CANCELLED.value:
        return OrderResultStatus.CANCELLED
    if event_name == DomainEventType.ORDER_REJECTED.value:
        return OrderResultStatus.REJECTED
    if event_name == DomainEventType.UNEXPECTED_RESTING_ORDER_DETECTED.value:
        return OrderResultStatus.LIVE
    return OrderResultStatus.UNKNOWN_TIMEOUT


def coerce_side(value: object | None) -> OrderSide | None:
    if value is None:
        return None
    try:
        if isinstance(value, OrderSide):
            return value
        return OrderSide(str(value).strip().upper())
    except Exception:
        return None


def coerce_order_type(value: object | None) -> OrderType | None:
    if value is None:
        return None
    try:
        if isinstance(value, OrderType):
            return value
        return OrderType(str(value).strip().upper())
    except Exception:
        return None


def decimal_or_none(value: object | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def market_from_result(order_result: OrderResult) -> Market | None:
    return Market(
        condition_id=order_result.condition_id,
        market_slug=order_result.market_slug or order_result.token_id,
        outcomes=(
            MarketOutcome(
                token_id=order_result.token_id,
                outcome="EXECUTED_OUTCOME",
            ),
        ),
    )


def result_event_type(order_result: OrderResult) -> DomainEventType:
    if order_result.status == OrderResultStatus.FULL_FILL:
        return DomainEventType.ORDER_MATCHED
    if order_result.status == OrderResultStatus.PARTIAL_FILL:
        return DomainEventType.ORDER_PARTIALLY_FILLED
    if order_result.status == OrderResultStatus.NO_FILL:
        return DomainEventType.ORDER_NO_FILL
    if order_result.status == OrderResultStatus.REJECTED:
        return DomainEventType.ORDER_REJECTED
    if order_result.status == OrderResultStatus.CANCELLED:
        return DomainEventType.ORDER_CANCELLED
    if order_result.status == OrderResultStatus.LIVE:
        return DomainEventType.ORDER_STATE_UPDATED
    return DomainEventType.ERROR
