"""验证 MarketDiscoveryWorker 在 universe 拒绝时也能落审计——CLAUDE.md §10。

修复前 ``MarketDiscoveryOutcome.should_publish_event`` 只为
``accepted/tracking_retained/tracking_removed`` 三种状态返回 True，导致
"首次看到 + 解析为目标盘口 + 但 universe 拒绝" 的市场静默丢弃，funnel /
analytics 端点完全看不见这一阶段拒绝原因。

本测试不复活整套 MarketService，直接给 worker 注入一个 fake，确保：
1. 首次发现的目标盘口被 universe 拒绝时仍会 publish MARKET_FILTERED_OUT
2. 同一市场重复发现时不会重复落审计（防噪）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from unittest.mock import MagicMock

from polymarket_trader.app.market_service import MarketDiscoveryOutcome
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.workers.market_discovery_worker import MarketDiscoveryWorker


@dataclass(frozen=True, slots=True)
class _FakeParseResult:
    accepted: bool
    status: Any
    reject_reason: Any = None
    reject_detail: str | None = None
    matched_keywords: tuple[str, ...] = ()
    matched_fields: tuple[str, ...] = ()
    condition_id: str | None = None
    market_slug: str | None = None
    event_slug: str | None = None


def _make_outcome(*, condition_id: str, market_slug: str) -> MarketDiscoveryOutcome:
    parse_result = _FakeParseResult(
        accepted=True,
        status=MagicMock(value="accepted"),
        condition_id=condition_id,
        market_slug=market_slug,
    )
    event = DomainEvent(
        trace_id="trace-discovery",
        event_type=DomainEventType.MARKET_FILTERED_OUT,
        event_id="evt-1",
        market_slug=market_slug,
        condition_id=condition_id,
        reason="universe_excluded",
        created_at=datetime.now(timezone.utc),
        payload={"market": {"condition_id": condition_id, "market_slug": market_slug}},
    )
    return MarketDiscoveryOutcome(
        trace_id="trace-discovery",
        source="test",
        parse_result=parse_result,
        event=event,
        market=None,
        discovery_kind=DomainEventType.MARKET_FILTERED_OUT.value,
        subscription_request=None,
        raw_market={"conditionId": condition_id, "slug": market_slug},
        existing_market=None,
        tracked_market=None,
        tracking_retained=False,
        tracking_removed=False,
        universe_decision=MagicMock(reason="universe_excluded", selected=False),
    )


class _StubEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[Any, DomainEvent]] = []

    async def publish(self, priority: Any, event: DomainEvent) -> None:
        self.published.append((priority, event))


class _FakeMarketService:
    def __init__(self, outcome: MarketDiscoveryOutcome) -> None:
        self._outcome = outcome
        self.call_count = 0

    def ingest_raw_market(self, raw_market: Mapping[str, Any], **_: Any) -> MarketDiscoveryOutcome:
        self.call_count += 1
        return self._outcome


def test_first_time_universe_rejection_emits_audit_event() -> None:
    outcome = _make_outcome(condition_id="0xcondA", market_slug="slug-A")
    bus = _StubEventBus()
    worker = MarketDiscoveryWorker(
        market_service=_FakeMarketService(outcome),
        event_bus=bus,
    )

    payload = {"markets": [{"conditionId": "0xcondA", "slug": "slug-A"}]}
    events = asyncio.run(worker.ingest_source_page(payload, source="test"))

    assert len(events) == 1, "首次发现的目标盘口被 universe 拒绝时必须发审计事件"
    assert events[0].event_type == DomainEventType.MARKET_FILTERED_OUT
    assert events[0].reason == "universe_excluded"
    assert len(bus.published) == 1


def test_repeated_universe_rejection_does_not_spam_audit() -> None:
    outcome = _make_outcome(condition_id="0xcondB", market_slug="slug-B")
    bus = _StubEventBus()
    worker = MarketDiscoveryWorker(
        market_service=_FakeMarketService(outcome),
        event_bus=bus,
    )

    payload = {"markets": [{"conditionId": "0xcondB", "slug": "slug-B"}]}
    asyncio.run(worker.ingest_source_page(payload, source="test"))
    asyncio.run(worker.ingest_source_page(payload, source="test"))

    assert len(bus.published) == 1, "重复发现同一市场不应该重复发审计事件"
