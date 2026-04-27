from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.order import OrderResultStatus, OrderSide
from polymarket_trader.infra.polymarket.order_execution_types import (
    OrderExecutionRequest,
    OrderExecutionResponse,
)
from polymarket_trader.infra.polymarket.order_signing import is_market_order_type


_FIXED_6_UNITS = Decimal("1000000")


def _text(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: Any | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    return Decimal(text)


def _fixed_6_decimal(value: Any | None) -> Decimal | None:
    amount = _decimal(value)
    if amount is None:
        return None
    return amount / _FIXED_6_UNITS


def response_reason(response: Mapping[str, Any]) -> str:
    return _text(
        response.get("reason")
        or response.get("errorMsg")
        or response.get("error_msg")
        or response.get("error")
    ) or ""


def response_order_id(response: Mapping[str, Any]) -> str | None:
    return _text(
        response.get("order_id")
        or response.get("orderID")
        or response.get("orderId")
        or response.get("id")
    )


def response_trade_id(response: Mapping[str, Any]) -> str | None:
    trade_id = _text(response.get("trade_id") or response.get("tradeID") or response.get("tradeId"))
    if trade_id is not None:
        return trade_id
    trade_ids = response.get("tradeIDs") or response.get("trade_ids")
    if isinstance(trade_ids, (list, tuple)) and trade_ids:
        return _text(trade_ids[0])
    return None


def map_submit_response(
    response: Mapping[str, Any],
    request: OrderExecutionRequest,
) -> OrderExecutionResponse:
    matched_shares, remaining_shares, spent_usdc, notional_usdc = _response_amounts(
        response,
        request,
    )
    return OrderExecutionResponse(
        status=_execution_status(response, request, matched_shares=matched_shares),
        order_id=response_order_id(response),
        trade_id=response_trade_id(response),
        matched_shares=(
            _decimal(response.get("matched_shares") or response.get("filled_shares"))
            or matched_shares
        ),
        remaining_shares=_decimal(response.get("remaining_shares")) or remaining_shares,
        spent_usdc=_decimal(response.get("spent_usdc") or response.get("amount_usdc")) or spent_usdc,
        notional_usdc=_decimal(response.get("notional_usdc")) or notional_usdc,
        raw_response=response,
        reason=response_reason(response) or "submitted",
        retryable=False,
    )


def _response_amounts(
    response: Mapping[str, Any],
    request: OrderExecutionRequest,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    making_amount = _fixed_6_decimal(response.get("makingAmount") or response.get("making_amount"))
    taking_amount = _fixed_6_decimal(response.get("takingAmount") or response.get("taking_amount"))
    raw_status = (_text(response.get("status")) or "").lower()
    terminal_match = raw_status == "matched"

    matched_shares: Decimal | None = None
    remaining_shares: Decimal | None = None
    spent_usdc: Decimal | None = None
    notional_usdc: Decimal | None = None

    if request.side == OrderSide.BUY:
        notional_usdc = making_amount
        if terminal_match:
            matched_shares = taking_amount
            spent_usdc = making_amount
            remaining_shares = _remaining_shares_after_match(request, matched_shares)
        elif raw_status in {"live", "delayed", "unmatched"}:
            remaining_shares = taking_amount
    elif request.side == OrderSide.SELL:
        notional_usdc = taking_amount
        if terminal_match:
            matched_shares = making_amount
            spent_usdc = taking_amount
            remaining_shares = _remaining_shares_after_match(request, matched_shares)
        elif raw_status in {"live", "delayed", "unmatched"}:
            remaining_shares = making_amount

    return matched_shares, remaining_shares, spent_usdc, notional_usdc


def _remaining_shares_after_match(
    request: OrderExecutionRequest,
    matched_shares: Decimal | None,
) -> Decimal | None:
    if matched_shares is None:
        return None
    if is_market_order_type(request.order_type):
        return Decimal("0")
    requested_shares = _requested_shares(request)
    if requested_shares is None:
        return None
    return max(requested_shares - matched_shares, Decimal("0"))


def _requested_shares(request: OrderExecutionRequest) -> Decimal | None:
    if request.side == OrderSide.BUY:
        if request.amount_usdc is None or request.price is None or request.price <= 0:
            return None
        return request.amount_usdc / request.price
    if request.side == OrderSide.SELL:
        return request.size_shares
    return None


def _execution_status(
    response: Mapping[str, Any],
    request: OrderExecutionRequest,
    *,
    matched_shares: Decimal | None,
) -> OrderResultStatus | str | None:
    raw_status = (_text(response.get("status")) or "").lower()
    reason = response_reason(response).lower()
    if _is_no_fill_response(reason, request):
        return OrderResultStatus.NO_FILL
    if response.get("success") is False or raw_status == "rejected" or reason:
        return OrderResultStatus.REJECTED
    if raw_status == "matched":
        return (
            OrderResultStatus.FULL_FILL
            if _is_full_fill(request, matched_shares)
            else OrderResultStatus.PARTIAL_FILL
        )
    if raw_status in {"live", "delayed", "unmatched"}:
        return OrderResultStatus.LIVE
    return _text(response.get("status"))


def _is_no_fill_response(reason: str, request: OrderExecutionRequest) -> bool:
    if not is_market_order_type(request.order_type):
        return False
    return (
        "no orders found to match with fak order" in reason
        or "couldn't be fully filled" in reason
        or "could not be fully filled" in reason
        or "fok orders are fully filled or killed" in reason
        or "fok orders are filled or killed" in reason
    )


def _is_full_fill(request: OrderExecutionRequest, matched_shares: Decimal | None) -> bool:
    if matched_shares is None:
        return True
    requested_shares = _requested_shares(request)
    if requested_shares is None or requested_shares <= 0:
        return True
    return matched_shares >= requested_shares - Decimal("0.000001")
