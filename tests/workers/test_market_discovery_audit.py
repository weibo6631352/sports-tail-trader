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


class _SwappableMarketService:
    """允许在两次 ingest 之间切换返回的 outcome——模拟 registry 短暂丢失 existing_market。"""

    def __init__(self, outcomes: list[MarketDiscoveryOutcome]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    def ingest_raw_market(self, _raw_market: Mapping[str, Any], **_: Any) -> MarketDiscoveryOutcome:
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        return outcome


def test_rediscovered_market_emits_market_updated_not_market_discovered() -> None:
    """N8：同一 market 二次发现时 event_type 必须降级为 MARKET_UPDATED。

    实测 audit_events 累积 41049 条 market_discovered vs 3586 markets，根因是
    discovery 缓存与 registry 丢失同步时，同一 condition_id 反复被分类为"首次发现"。
    worker 自己曾经见过 + outcome.accepted 时必须把 event_type 降级。

    这里第二次调用故意返回 existing_market=None（registry 看起来失忆），让 market
    service 算出来的 discovery_kind 还是 MARKET_DISCOVERED；只要 worker 自己曾经
    见过这条 condition_id，event_type 必须被降级为 MARKET_UPDATED。
    """

    # 共用同一个 mock 作为 market，避免 MagicMock 实例不等导致 dedup 路径被
    # 误触发 unchanged_payload && previous_market == current_market 短路。
    shared_market = MagicMock(
        condition_id="0xcondC",
        market_slug="slug-C",
        token_ids=("token-yes", "token-no"),
    )

    def _outcome() -> MarketDiscoveryOutcome:
        parse_result = _FakeParseResult(
            accepted=True,
            status=MagicMock(value="accepted"),
            condition_id="0xcondC",
            market_slug="slug-C",
        )
        event = DomainEvent(
            trace_id="trace-discovery",
            event_type=DomainEventType.MARKET_DISCOVERED,
            event_id="evt-1",
            market_slug="slug-C",
            condition_id="0xcondC",
            reason="",
            created_at=datetime.now(timezone.utc),
            payload={"market": {"condition_id": "0xcondC", "market_slug": "slug-C"}},
        )
        return MarketDiscoveryOutcome(
            trace_id="trace-discovery",
            source="test",
            parse_result=parse_result,
            event=event,
            market=shared_market,
            discovery_kind=DomainEventType.MARKET_DISCOVERED.value,
            subscription_request=None,
            raw_market={"conditionId": "0xcondC", "slug": "slug-C"},
            existing_market=None,
            tracked_market=shared_market,
            tracking_retained=False,
            tracking_removed=False,
            universe_decision=MagicMock(reason="universe_accepted", selected=True),
        )

    bus = _StubEventBus()
    worker = MarketDiscoveryWorker(
        market_service=_SwappableMarketService([_outcome(), _outcome()]),
        event_bus=bus,
    )
    payload = {"markets": [{"conditionId": "0xcondC", "slug": "slug-C"}]}

    asyncio.run(worker.ingest_source_page(payload, source="test"))
    asyncio.run(worker.ingest_source_page(payload, source="test"))

    # 第一次 ingest 正常发 MARKET_DISCOVERED；第二次必须降级为 MARKET_UPDATED，
    # 不能再发 market_discovered 噪音。
    assert len(bus.published) == 2
    assert bus.published[0][1].event_type == DomainEventType.MARKET_DISCOVERED
    assert bus.published[1][1].event_type == DomainEventType.MARKET_UPDATED
    assert bus.published[1][1].payload["discovery_kind"] == DomainEventType.MARKET_UPDATED.value
