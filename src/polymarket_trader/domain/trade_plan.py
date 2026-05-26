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
class TradePlan:
    """单 market tick 的完整交易决策结果（值对象 / 内部 DTO）。

    含决策上下文（market + orderbook）、策略输出（summary + decision_kind）、
    预算分配（allocation_plan + allocation）、最终下单意图（intent）。
    `intent` 可能是 Buy/Sell/Cancel/Replace 中任一种——本 DTO 不是"入场专属"，
    覆盖 entry/exit/skip/raise/reduce 所有 decision_kind。

    `ready_to_trade=True` 时调用方可直接交给 OrderGateway 执行。
    """

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
