from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from polymarket_trader.domain.order import ExecutionTimestamps, OrderResultStatus, OrderSide, OrderType


def request_signature(request: OrderExecutionRequest) -> str:
    return "|".join(
        [
            request.action,
            request.trace_id,
            request.idempotency_key,
            request.condition_id,
            request.token_id,
            request.market_slug or "",
            request.side.value if request.side is not None else "",
            request.order_type.value if request.order_type is not None else "",
            "" if request.price is None else str(request.price),
            "" if request.amount_usdc is None else str(request.amount_usdc),
            "" if request.size_shares is None else str(request.size_shares),
            request.order_id or "",
            "" if request.new_price is None else str(request.new_price),
            str(int(request.post_only)),
        ]
    )


@dataclass(frozen=True, slots=True)
class OrderExecutionRequest:
    action: str
    trace_id: str
    idempotency_key: str
    condition_id: str
    token_id: str
    market_slug: str | None = None
    side: OrderSide | None = None
    order_type: OrderType | None = None
    price: Decimal | None = None
    amount_usdc: Decimal | None = None
    size_shares: Decimal | None = None
    order_id: str | None = None
    new_price: Decimal | None = None
    post_only: bool = False
    reason: str = ""
    timestamps: ExecutionTimestamps = field(default_factory=ExecutionTimestamps)

    def fingerprint(self) -> str:
        return request_signature(self)


@dataclass(frozen=True, slots=True)
class OrderExecutionResponse:
    status: OrderResultStatus | str | None = None
    order_id: str | None = None
    trade_id: str | None = None
    matched_shares: Decimal | str | None = None
    remaining_shares: Decimal | str | None = None
    spent_usdc: Decimal | str | None = None
    notional_usdc: Decimal | str | None = None
    raw_response: Any | None = None
    reason: str = ""
    retryable: bool = False
    timestamps: ExecutionTimestamps | None = None


@runtime_checkable
class OrderExecutionClient(Protocol):
    def sign_order(self, request: OrderExecutionRequest) -> Any: ...

    def submit_order(self, request: OrderExecutionRequest) -> Any: ...

    def cancel_order(self, request: OrderExecutionRequest) -> Any: ...

    def replace_order(self, request: OrderExecutionRequest) -> Any: ...
