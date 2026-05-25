from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.events import sanitize_raw_response
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    ExecutionTimestamps,
    ManagedOrderIntent,
    OrderResult,
    OrderResultStatus,
    OrderType,
    SellOrderIntent,
)
from polymarket_trader.infra.polymarket.order_execution_types import (
    OrderExecutionRequest,
    OrderExecutionResponse,
)


def normalize_execution_response(
    response: Any,
    *,
    action: str,
    intent: ManagedOrderIntent,
    timestamps: ExecutionTimestamps,
) -> OrderExecutionResponse:
    if isinstance(response, OrderExecutionResponse):
        return response
    if isinstance(response, OrderResult):
        return OrderExecutionResponse(
            status=response.status,
            order_id=response.order_id,
            trade_id=response.trade_id,
            matched_shares=response.matched_shares,
            remaining_shares=response.remaining_shares,
            spent_usdc=response.spent_usdc,
            notional_usdc=response.notional_usdc,
            raw_response=response.raw_response_summary,
            reason=response.reason,
            retryable=response.retryable,
            timestamps=response.timestamps,
        )
    if isinstance(response, Mapping):
        return OrderExecutionResponse(
            status=response.get("status"),
            order_id=_as_text(response.get("order_id")),
            trade_id=_as_text(response.get("trade_id")),
            matched_shares=_decimal(response.get("matched_shares") or response.get("filled_shares")),
            remaining_shares=_decimal(response.get("remaining_shares")),
            spent_usdc=_decimal(response.get("spent_usdc") or response.get("amount_usdc")),
            notional_usdc=_decimal(response.get("notional_usdc")),
            raw_response=response,
            reason=_as_text(response.get("reason")) or "",
            retryable=bool(response.get("retryable", False)),
            timestamps=timestamps,
        )
    return OrderExecutionResponse(
        status=_coerce_status(None, action=action, intent=intent),
        raw_response=response,
        reason=_as_text(getattr(response, "reason", None)) or "",
        timestamps=timestamps,
    )


def build_order_result(
    *,
    request: OrderExecutionRequest,
    intent: ManagedOrderIntent,
    response: OrderExecutionResponse,
    timestamps: ExecutionTimestamps,
) -> OrderResult:
    status = _coerce_status(response.status, action=request.action, intent=intent)
    matched_shares = _decimal(response.matched_shares) or Decimal("0")
    remaining_shares = _decimal(response.remaining_shares) or Decimal("0")
    spent_usdc = _decimal(response.spent_usdc) or Decimal("0")
    notional_usdc = _decimal(response.notional_usdc) or Decimal("0")
    price = request.price or request.new_price

    if isinstance(intent, BuyOrderIntent):
        requested_amount_usdc = intent.amount_usdc
        requested_size_shares = None
        notional_usdc = notional_usdc or intent.amount_usdc
        if (
            notional_usdc
            and status in {OrderResultStatus.FULL_FILL, OrderResultStatus.PARTIAL_FILL}
            and spent_usdc == Decimal("0")
        ):
            spent_usdc = notional_usdc
        if (
            status in {OrderResultStatus.FULL_FILL, OrderResultStatus.PARTIAL_FILL}
            and matched_shares == Decimal("0")
            and spent_usdc
            and price
        ):
            matched_shares = spent_usdc / price
        if status == OrderResultStatus.NO_FILL:
            remaining_shares = Decimal("0")
    elif isinstance(intent, SellOrderIntent):
        requested_amount_usdc = None
        requested_size_shares = intent.size_shares
        notional_usdc = notional_usdc or intent.notional_usdc
        if (
            status in {OrderResultStatus.FULL_FILL, OrderResultStatus.PARTIAL_FILL}
            and spent_usdc == Decimal("0")
            and price
        ):
            spent_usdc = price * (matched_shares or intent.size_shares)
        if (
            status in {OrderResultStatus.FULL_FILL, OrderResultStatus.PARTIAL_FILL}
            and matched_shares == Decimal("0")
        ):
            matched_shares = intent.size_shares - remaining_shares if remaining_shares else intent.size_shares
    else:
        requested_amount_usdc = None
        requested_size_shares = None

    if status == OrderResultStatus.CANCELLED:
        matched_shares = Decimal("0")
        remaining_shares = Decimal("0")
        spent_usdc = Decimal("0")

    return OrderResult(
        trace_id=request.trace_id,
        condition_id=request.condition_id,
        token_id=request.token_id,
        market_slug=request.market_slug,
        status=status,
        intent=intent,
        order_id=response.order_id,
        trade_id=response.trade_id,
        side=request.side,
        order_type=request.order_type,
        price=price,
        requested_amount_usdc=requested_amount_usdc,
        requested_size_shares=requested_size_shares,
        matched_shares=matched_shares,
        remaining_shares=remaining_shares,
        spent_usdc=spent_usdc,
        notional_usdc=notional_usdc,
        reason=response.reason or _default_reason(status, request.action),
        retryable=response.retryable,
        raw_response_summary=sanitize_raw_response(response.raw_response),
        timestamps=timestamps,
    )


def _decimal(value: Any | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    return Decimal(text)


def _as_text(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_status(value: Any | None, *, action: str, intent: ManagedOrderIntent | None) -> OrderResultStatus:
    if isinstance(value, OrderResultStatus):
        return value
    if value is None:
        return _default_status_for_action(action, intent)
    text = str(value).strip().lower().replace(" ", "_")
    mapping = {
        "full_fill": OrderResultStatus.FULL_FILL,
        "full": OrderResultStatus.FULL_FILL,
        "filled": OrderResultStatus.FULL_FILL,
        "partial_fill": OrderResultStatus.PARTIAL_FILL,
        "partial": OrderResultStatus.PARTIAL_FILL,
        "partially_filled": OrderResultStatus.PARTIAL_FILL,
        "no_fill": OrderResultStatus.NO_FILL,
        "nofill": OrderResultStatus.NO_FILL,
        "live": OrderResultStatus.LIVE,
        "resting": OrderResultStatus.LIVE,
        "rejected": OrderResultStatus.REJECTED,
        "failed": OrderResultStatus.FAILED,
        "cancelled": OrderResultStatus.CANCELLED,
        "canceled": OrderResultStatus.CANCELLED,
        "unknown_timeout": OrderResultStatus.UNKNOWN_TIMEOUT,
        "timeout": OrderResultStatus.UNKNOWN_TIMEOUT,
    }
    if text in mapping:
        return mapping[text]
    return _default_status_for_action(action, intent)


def _default_status_for_action(
    action: str,
    intent: ManagedOrderIntent | None,
) -> OrderResultStatus:
    if action == "cancel":
        return OrderResultStatus.CANCELLED
    if action == "replace":
        return OrderResultStatus.LIVE
    if isinstance(intent, BuyOrderIntent):
        return OrderResultStatus.NO_FILL if intent.order_type == OrderType.FAK else OrderResultStatus.LIVE
    if isinstance(intent, SellOrderIntent):
        return OrderResultStatus.LIVE
    return OrderResultStatus.FAILED


def _default_reason(status: OrderResultStatus, action: str) -> str:
    if status == OrderResultStatus.UNKNOWN_TIMEOUT:
        return f"{action}_timeout"
    if status == OrderResultStatus.CANCELLED:
        return "cancelled"
    if status == OrderResultStatus.NO_FILL:
        return "no_fill"
    if status == OrderResultStatus.FULL_FILL:
        return "full_fill"
    if status == OrderResultStatus.PARTIAL_FILL:
        return "partial_fill"
    if status == OrderResultStatus.LIVE:
        return "live"
    if status == OrderResultStatus.REJECTED:
        return "rejected"
    return "failed"
