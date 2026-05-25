from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import TradableOrderIntent
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.decisions import DecisionKind
from polymarket_trader.domain.decisions import StrategySummary


@dataclass(frozen=True, slots=True)
class EntryPlan:
    trace_id: str
    market: Market | None
    orderbook: OrderbookSnapshot | None
    allocation_plan: AllocationPlan
    allocation: Allocation | None
    intent: TradableOrderIntent | None
    eligible_market_count: int = 0
    reason: str = ""
    decision_kind: DecisionKind | None = None
    summary: StrategySummary | None = None
    metadata: Mapping[str, Any] | None = None

    @property
    def ready_to_trade(self) -> bool:
        return self.intent is not None and self.allocation is not None and self.market is not None
