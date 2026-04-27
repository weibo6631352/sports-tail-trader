from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from polymarket_trader.app.trading_decision_service import EntryPlan
from polymarket_trader.app.trading_service import TradingReviewResult
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    ManagedOrderIntent,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderType,
)
from polymarket_trader.domain.position import Position
from polymarket_trader.serialization import jsonable

TRADING_DECISION_WORKER_ORIGIN = "trading_decision_worker"


def is_self_emitted(event: DomainEvent) -> bool:
    return event.payload.get("origin") == TRADING_DECISION_WORKER_ORIGIN


def serialize_snapshot(snapshot: AccountSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "balance_usdc": str(snapshot.balance_usdc),
        "allowance_usdc": str(snapshot.allowance_usdc),
        "user_ws_connected": snapshot.user_ws_connected,
        "allow_new_entries": snapshot.allow_new_entries,
        "market_pauses": [pause.as_payload() for pause in snapshot.market_pauses],
        "last_reconcile_at": (
            None if snapshot.last_reconcile_at is None else snapshot.last_reconcile_at.isoformat()
        ),
        "positions": [
            {
                "condition_id": position.condition_id,
                "token_id": position.token_id,
                "shares": str(position.shares),
                "cost_usdc": str(position.cost_usdc),
                "open_buy_shares": str(position.open_buy_shares),
                "open_sell_shares": str(position.open_sell_shares),
                "pending_buy_shares": str(position.pending_buy_shares),
            }
            for position in snapshot.positions
        ],
        "open_orders": [
            {
                "condition_id": order.condition_id,
                "token_id": order.token_id,
                "side": order.side.value,
                "status": order.status.value,
                "order_id": order.order_id,
                "idempotency_key": order.idempotency_key,
                "remaining_shares": (
                    None if order.remaining_shares is None else str(order.remaining_shares)
                ),
            }
            for order in snapshot.open_orders
        ],
    }


def serialize_allocation_plan(plan: EntryPlan) -> dict[str, object]:
    return {
        "trace_id": plan.allocation_plan.trace_id,
        "total_budget_usdc": str(plan.allocation_plan.total_budget_usdc),
        "allocated_budget_usdc": str(plan.allocation_plan.allocated_budget_usdc),
        "released_budget_usdc": str(plan.allocation_plan.released_budget_usdc),
        "eligible_market_count": plan.eligible_market_count,
        "reason": plan.allocation_plan.reason,
    }


def serialize_allocation(plan: EntryPlan) -> dict[str, object] | None:
    if plan.allocation is None:
        return None
    return {
        "condition_id": plan.allocation.condition_id,
        "target_budget_usdc": str(plan.allocation.target_budget_usdc),
        "buy_budget_usdc": str(plan.allocation.buy_budget_usdc),
        "current_exposure_usdc": str(plan.allocation.current_exposure_usdc),
        "released_budget_usdc": str(plan.allocation.released_budget_usdc),
        "reason": plan.allocation.reason,
        "release_reason": plan.allocation.release_reason,
    }


def serialize_plan_metadata(plan: EntryPlan) -> dict[str, object]:
    """序列化入场计划的审计 metadata。

    metadata 由 app 层透传，可能包含策略候选原因、执行权限或事件输入。
    这里仅做 JSON 友好转换，不解释具体策略字段。
    """

    metadata = jsonable(plan.metadata or {})
    return metadata if isinstance(metadata, dict) else {"value": metadata}


def serialize_intent(intent: ManagedOrderIntent) -> dict[str, object]:
    amount_usdc = getattr(intent, "amount_usdc", None)
    size_shares = getattr(intent, "size_shares", None)
    return {
        "trace_id": intent.trace_id,
        "condition_id": intent.condition_id,
        "token_id": intent.token_id,
        "side": intent.side.value if hasattr(intent, "side") else None,
        "order_type": intent.order_type.value if hasattr(intent, "order_type") else None,
        "price": str(intent.price) if hasattr(intent, "price") and intent.price is not None else None,
        "amount_usdc": None if amount_usdc is None else str(amount_usdc),
        "size_shares": None if size_shares is None else str(size_shares),
        "order_id": getattr(intent, "order_id", None),
        "reason": getattr(intent, "reason", ""),
        "market_slug": intent.market_slug,
    }


def serialize_review(review: TradingReviewResult) -> dict[str, object]:
    return {
        "operation": review.operation,
        "submitted": review.submitted,
        "submission_error": review.submission_error,
        "risk_decision": None
        if review.risk_decision is None
        else {
            "passed": review.risk_decision.passed,
            "reason": review.risk_decision.reason,
            "failed_field": review.risk_decision.failed_field,
            "suggested_action": review.risk_decision.suggested_action,
            "retryable": review.risk_decision.retryable,
        },
        "order_result": serialize_order_result(review.order_result)
        if review.order_result is not None
        else None,
    }


def serialize_control_intent(intent: CancelOrderIntent) -> dict[str, object]:
    return {
        "trace_id": intent.trace_id,
        "condition_id": intent.condition_id,
        "token_id": intent.token_id,
        "order_id": intent.order_id,
        "market_slug": intent.market_slug,
        "reason": intent.reason,
    }


def serialize_order_result(order_result: OrderResult | None) -> dict[str, object] | None:
    if order_result is None:
        return None
    return {
        "trace_id": order_result.trace_id,
        "condition_id": order_result.condition_id,
        "token_id": order_result.token_id,
        "status": order_result.status.value,
        "market_slug": order_result.market_slug,
        "order_id": order_result.order_id,
        "trade_id": order_result.trade_id,
        "side": None if order_result.side is None else order_result.side.value,
        "order_type": None if order_result.order_type is None else order_result.order_type.value,
        "price": None if order_result.price is None else str(order_result.price),
        "requested_amount_usdc": None
        if order_result.requested_amount_usdc is None
        else str(order_result.requested_amount_usdc),
        "requested_size_shares": None
        if order_result.requested_size_shares is None
        else str(order_result.requested_size_shares),
        "matched_shares": str(order_result.matched_shares),
        "remaining_shares": str(order_result.remaining_shares),
        "spent_usdc": str(order_result.spent_usdc),
        "notional_usdc": str(order_result.notional_usdc),
        "reason": order_result.reason,
        "retryable": order_result.retryable,
        "raw_response_summary": order_result.raw_response_summary,
    }


def coerce_order_result_from_event(event: DomainEvent) -> OrderResult | None:
    payload = dict(event.payload)
    order_result_payload = payload.get("order_result")
    if isinstance(order_result_payload, Mapping):
        payload = {**payload, **dict(order_result_payload)}
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
        requested_amount_usdc=decimal_or_none(payload.get("requested_amount_usdc")),
        requested_size_shares=decimal_or_none(payload.get("requested_size_shares")),
        matched_shares=decimal_or_none(payload.get("matched_shares")) or Decimal("0"),
        remaining_shares=decimal_or_none(payload.get("remaining_shares")) or Decimal("0"),
        spent_usdc=decimal_or_none(payload.get("spent_usdc")) or Decimal("0"),
        notional_usdc=decimal_or_none(payload.get("notional_usdc")) or Decimal("0"),
        reason=str(payload.get("reason", event.reason or "")),
        retryable=bool(payload.get("retryable", False)),
        raw_response_summary=payload.get("raw_response_summary"),
    )


def coerce_status(
    status_value: object | None,
    event_type: DomainEventType | str,
) -> OrderResultStatus:
    if isinstance(status_value, OrderResultStatus):
        return status_value
    if isinstance(status_value, str):
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
        return OrderSide(str(value))
    except Exception:
        return None


def coerce_order_type(value: object | None) -> OrderType | None:
    if value is None:
        return None
    try:
        if isinstance(value, OrderType):
            return value
        return OrderType(str(value))
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


def snapshot_balance(snapshot: AccountSnapshot | None) -> Decimal:
    if snapshot is None:
        return Decimal("0")
    return snapshot.balance_usdc


def snapshot_allowance(snapshot: AccountSnapshot | None) -> Decimal:
    if snapshot is None:
        return Decimal("0")
    return snapshot.allowance_usdc


def snapshot_position(
    snapshot: AccountSnapshot | None,
    condition_id: str,
    token_id: str,
) -> Position | None:
    if snapshot is None:
        return None
    return snapshot.get_position(condition_id, token_id)


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
