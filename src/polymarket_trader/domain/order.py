from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, TypeAlias


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    FAK = "FAK"
    GTC = "GTC"


class OrderStatus(StrEnum):
    CREATED = "created"
    SIGNED = "signed"
    SUBMITTED = "submitted"
    CANCEL_REQUESTED = "cancel_requested"
    MATCHED = "matched"
    PARTIALLY_FILLED = "partially_filled"
    NO_FILL = "no_fill"
    LIVE = "live"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class OrderResultStatus(StrEnum):
    FULL_FILL = "full_fill"
    PARTIAL_FILL = "partial_fill"
    NO_FILL = "no_fill"
    LIVE = "live"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN_TIMEOUT = "unknown_timeout"


@dataclass(frozen=True, slots=True)
class ExecutionTimestamps:
    queued_at: datetime | None = None
    sign_started_at: datetime | None = None
    signed_at: datetime | None = None
    submitted_at: datetime | None = None
    ack_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BuyOrderIntent:
    trace_id: str
    condition_id: str
    token_id: str
    price: Decimal
    amount_usdc: Decimal
    market_slug: str | None = None
    order_type: OrderType = OrderType.FAK
    idempotency_key: str | None = None
    post_only: bool = False
    retry_count: int = 0
    allow_open_exit_overlap: bool = False
    intent_tags: frozenset[str] = field(default_factory=frozenset)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def side(self) -> OrderSide:
        return OrderSide.BUY

    @property
    def size_shares(self) -> Decimal | None:
        return None

    @property
    def notional_usdc(self) -> Decimal:
        return self.amount_usdc


@dataclass(frozen=True, slots=True)
class SellOrderIntent:
    trace_id: str
    condition_id: str
    token_id: str
    price: Decimal
    size_shares: Decimal
    market_slug: str | None = None
    order_type: OrderType = OrderType.GTC
    idempotency_key: str | None = None
    post_only: bool = False
    retry_count: int = 0
    intent_tags: frozenset[str] = field(default_factory=frozenset)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def side(self) -> OrderSide:
        return OrderSide.SELL

    @property
    def amount_usdc(self) -> Decimal | None:
        return None

    @property
    def notional_usdc(self) -> Decimal:
        return self.price * self.size_shares


@dataclass(frozen=True, slots=True)
class CancelOrderIntent:
    trace_id: str
    condition_id: str
    token_id: str
    order_id: str
    market_slug: str | None = None
    idempotency_key: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReplaceOrderIntent:
    trace_id: str
    condition_id: str
    token_id: str
    order_id: str
    new_price: Decimal
    size_shares: Decimal
    market_slug: str | None = None
    idempotency_key: str | None = None
    reason: str = ""


TradableOrderIntent: TypeAlias = BuyOrderIntent | SellOrderIntent
OrderControlIntent: TypeAlias = CancelOrderIntent | ReplaceOrderIntent
OrderIntent: TypeAlias = TradableOrderIntent
ManagedOrderIntent: TypeAlias = TradableOrderIntent | OrderControlIntent


@dataclass(frozen=True, slots=True)
class OrderRecord:
    condition_id: str
    token_id: str
    side: OrderSide
    order_type: OrderType
    price: Decimal
    trace_id: str = ""
    market_slug: str | None = None
    amount_usdc: Decimal | None = None
    size_shares: Decimal | None = None
    filled_shares: Decimal = Decimal("0")
    remaining_shares: Decimal | None = None
    notional_usdc: Decimal | None = None
    order_id: str | None = None
    trade_id: str | None = None
    status: OrderStatus = OrderStatus.CREATED
    idempotency_key: str | None = None
    reason: str = ""
    post_only: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.status in {
            OrderStatus.CREATED,
            OrderStatus.SIGNED,
            OrderStatus.SUBMITTED,
            OrderStatus.CANCEL_REQUESTED,
            OrderStatus.LIVE,
            OrderStatus.MATCHED,
            OrderStatus.PARTIALLY_FILLED,
        }


Order = OrderRecord


@dataclass(frozen=True, slots=True)
class OrderResult:
    trace_id: str
    condition_id: str
    token_id: str
    status: OrderResultStatus
    intent: ManagedOrderIntent | None = None
    market_slug: str | None = None
    order_id: str | None = None
    trade_id: str | None = None
    side: OrderSide | None = None
    order_type: OrderType | None = None
    price: Decimal | None = None
    requested_amount_usdc: Decimal | None = None
    requested_size_shares: Decimal | None = None
    matched_shares: Decimal = Decimal("0")
    remaining_shares: Decimal = Decimal("0")
    spent_usdc: Decimal = Decimal("0")
    notional_usdc: Decimal = Decimal("0")
    reason: str = ""
    retryable: bool = False
    raw_response_summary: str | None = None
    timestamps: ExecutionTimestamps = field(default_factory=ExecutionTimestamps)

    @property
    def filled(self) -> bool:
        return self.status in {
            OrderResultStatus.FULL_FILL,
            OrderResultStatus.PARTIAL_FILL,
        }

    @property
    def has_resting_order(self) -> bool:
        return self.status == OrderResultStatus.LIVE
