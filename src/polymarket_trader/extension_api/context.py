from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Protocol

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.account import MarketPause
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order, OrderResult
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api.decisions import EntryCandidate, MarketTokenView


class AccountSnapshotView(Protocol):
    @property
    def balance_usdc(self) -> Decimal: ...

    @property
    def allowance_usdc(self) -> Decimal: ...

    @property
    def available_usdc(self) -> Decimal: ...

    @property
    def positions(self) -> tuple[Position, ...]: ...

    @property
    def open_orders(self) -> tuple[Order, ...]: ...

    @property
    def allow_new_entries(self) -> bool: ...

    @property
    def market_pauses(self) -> tuple[MarketPause, ...]: ...

    @property
    def last_reconcile_at(self) -> datetime | None: ...

    def get_position(self, condition_id: str, token_id: str) -> Position | None: ...

    def is_market_paused(self, condition_id: str) -> bool: ...

    def open_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]: ...

    def open_buy_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]: ...

    def open_sell_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]: ...


@dataclass(frozen=True, slots=True)
class ExtensionContext:
    trace_id: str
    market: Market | None = None
    token_id: str | None = None
    orderbook: OrderbookSnapshot | None = None
    market_token_views: tuple[MarketTokenView, ...] = ()
    account_snapshot: AccountSnapshotView | None = None
    position: Position | None = None
    open_orders: tuple[Order, ...] = ()
    entry_candidates: tuple[EntryCandidate, ...] = ()
    order_result: OrderResult | None = None
    now: datetime | None = None
    portfolio_budget_usdc: Decimal | None = None
    available_usdc: Decimal | None = None
    max_order_usdc: Decimal | None = None
    max_market_usdc: Decimal | None = None
    max_total_usdc: Decimal | None = None
    allocation_plan: AllocationPlan | None = None
    allocation: Allocation | None = None
    amount_usdc: Decimal | None = None
    size_shares: Decimal | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
