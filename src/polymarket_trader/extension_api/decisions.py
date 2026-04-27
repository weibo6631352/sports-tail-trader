from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position


class ExtensionAction(StrEnum):
    SKIP = "skip"
    BUY = "buy"
    SELL = "sell"
    CANCEL = "cancel"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class MarketTokenView:
    token_id: str
    outcome: str
    orderbook: OrderbookSnapshot | None = None
    position: Position | None = None
    open_orders: tuple[Order, ...] = ()


@dataclass(frozen=True, slots=True)
class UniverseDecision:
    selected: bool
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def include(
        cls,
        *,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "UniverseDecision":
        return cls(selected=True, reason=reason, metadata=metadata or {})

    @classmethod
    def exclude(
        cls,
        *,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> "UniverseDecision":
        return cls(selected=False, reason=reason, metadata=metadata or {})


@dataclass(frozen=True, slots=True)
class EntryCandidate:
    market: Market
    token_id: str
    orderbook: OrderbookSnapshot
    position: Position | None = None
    open_orders: tuple[Order, ...] = ()
    idempotency_key: str | None = None

    @property
    def condition_id(self) -> str:
        return self.market.condition_id


@dataclass(frozen=True, slots=True)
class ExtensionDecision:
    action: ExtensionAction
    reason: str = ""
    token_id: str | None = None
    price: Decimal | None = None
    amount_usdc: Decimal | None = None
    size_shares: Decimal | None = None
    order_id: str | None = None
    order_type: OrderType | None = None
    market_slug: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def skip(
        cls,
        *,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(action=ExtensionAction.SKIP, reason=reason, metadata=metadata or {})

    @classmethod
    def buy(
        cls,
        *,
        reason: str,
        token_id: str | None,
        price: Decimal,
        amount_usdc: Decimal,
        order_type: OrderType | None = None,
        market_slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(
            action=ExtensionAction.BUY,
            reason=reason,
            token_id=token_id,
            price=price,
            amount_usdc=amount_usdc,
            order_type=order_type,
            market_slug=market_slug,
            metadata=metadata or {},
        )

    @classmethod
    def sell(
        cls,
        *,
        reason: str,
        token_id: str | None,
        price: Decimal,
        size_shares: Decimal,
        order_type: OrderType | None = None,
        market_slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(
            action=ExtensionAction.SELL,
            reason=reason,
            token_id=token_id,
            price=price,
            size_shares=size_shares,
            order_type=order_type,
            market_slug=market_slug,
            metadata=metadata or {},
        )

    @classmethod
    def cancel(
        cls,
        *,
        reason: str,
        token_id: str | None,
        order_id: str,
        market_slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(
            action=ExtensionAction.CANCEL,
            reason=reason,
            token_id=token_id,
            order_id=order_id,
            market_slug=market_slug,
            metadata=metadata or {},
        )

    @classmethod
    def replace(
        cls,
        *,
        reason: str,
        token_id: str | None,
        order_id: str,
        price: Decimal,
        size_shares: Decimal,
        market_slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(
            action=ExtensionAction.REPLACE,
            reason=reason,
            token_id=token_id,
            order_id=order_id,
            price=price,
            size_shares=size_shares,
            market_slug=market_slug,
            metadata=metadata or {},
        )


@dataclass(frozen=True, slots=True)
class EntrySizing:
    allocation_plan: AllocationPlan
    allocation: Allocation | None = None
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def eligible_market_count(self) -> int:
        return self.allocation_plan.eligible_market_count


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    reason: str = ""
    actions: tuple[ExtensionDecision, ...] = ()
    pause_trading: bool = False
    pause_reason: str = ""

    @property
    def has_actions(self) -> bool:
        return bool(self.actions or self.pause_trading)
