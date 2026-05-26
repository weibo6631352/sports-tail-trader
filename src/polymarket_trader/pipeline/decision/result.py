from __future__ import annotations

from dataclasses import dataclass

from polymarket_trader.pipeline.decision.decision_context_builder import EntryPlan
from polymarket_trader.pipeline.execution.order_gateway import OrderGatewayReview
from polymarket_trader.domain.events import DomainEvent
from polymarket_trader.domain.order import ManagedOrderIntent
from polymarket_trader.domain.state_machine import MarketLifecycle


@dataclass(frozen=True, slots=True)
class MarketTickWorkerResult:
    entry_event: DomainEvent
    plan: EntryPlan | None
    review: OrderGatewayReview | None
    emitted_event: DomainEvent | None
    emitted_events: tuple[DomainEvent, ...] = ()
    follow_up_intents: tuple[ManagedOrderIntent, ...] = ()
    follow_up_reviews: tuple[OrderGatewayReview, ...] = ()
    state_before: MarketLifecycle | None = None
    state_after: MarketLifecycle | None = None
