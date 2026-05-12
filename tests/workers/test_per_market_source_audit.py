"""缺口 1 接线测试：worker audit/event payload 含 per-market 源选择信息。

P0 路径只走 outbox put_nowait（P1 entry signal）；audit (P3) 延迟异步发布，
但必须把 primary_source / contributing_sources / confidence / source_conflicts
带出来，让复盘能看到"该 market 在那一刻实际是哪些源融合的"。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.sports_live import (
    ConflictRecord,
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
)
from polymarket_trader.extension_api.live_state import LiveStateMatch
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.sports_live_state_worker import SportsLiveStateWorker


_OBSERVED = datetime(2026, 5, 11, tzinfo=timezone.utc)


def _market() -> Market:
    return Market(
        condition_id="moneyline-condition",
        market_slug="nba-nyk-bos-moneyline",
        market_question="Will the Knicks beat the Celtics?",
        event_title="New York Knicks vs. Boston Celtics",
        event_slug="new-york-knicks-vs-boston-celtics",
        category="Sports",
        tags=("NBA", "Basketball"),
        outcomes=(
            MarketOutcome(token_id="home", outcome="New York Knicks"),
            MarketOutcome(token_id="away", outcome="Boston Celtics"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _event_with_conflicts() -> LiveEvent:
    return LiveEvent(
        source="nba",
        source_event_id="game-1",
        kind=LiveEventKind.TEAM_MATCH,
        league="NBA",
        sport="basketball",
        participants=(
            Participant(role="home", name="Knicks", score=102, display_name="New York Knicks", abbreviation="NYK", short_name="Knicks"),
            Participant(role="away", name="Celtics", score=94, display_name="Boston Celtics", abbreviation="BOS", short_name="Celtics"),
        ),
        status=SportsLiveGameStatus.LIVE,
        period="Q4",
        seconds_remaining=90,
        observed_at=_OBSERVED,
        contributing_sources=("nba", "espn"),
        source_conflicts=(
            ConflictRecord(
                field="status",
                winner_source="nba",
                winner_value=SportsLiveGameStatus.LIVE.value,
                loser_source="espn",
                loser_value=SportsLiveGameStatus.PAUSED.value,
                decided_by="majority",
            ),
        ),
    )


def _live_match(market: Market, event: LiveEvent) -> LiveStateMatch:
    return LiveStateMatch(
        market=market,
        event=event,
        signal_allowed=True,
        signal_reason="within_tail_window",
        phase="live",
        primary_source="nba",
        contributing_sources=("nba", "espn"),
        confidence=0.7,
        payload={
            "live_game": {
                "league": "NBA",
                "home_name": "Knicks",
                "away_name": "Celtics",
                "home_score": 102,
                "away_score": 94,
                "period": "Q4",
                "seconds_remaining": 90,
                "status": "live",
            }
        },
    )


def test_audit_payload_includes_primary_and_contributing_sources_and_conflicts() -> None:
    """audit_events 必须带 per-market primary_source/contributing_sources/confidence/conflicts。"""

    async def run() -> dict[str, object]:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        event = _event_with_conflicts()
        event_bus = EventBus(trading_capacity=10, maintenance_capacity=10, persistence_capacity=10)

        async def provider() -> SportsLiveSnapshot:
            return SportsLiveSnapshot(source="sports_live_aggregate", observed_at=_OBSERVED, events=(event,))

        worker = SportsLiveStateWorker(
            snapshot_provider=provider,
            match_live_state=lambda m, _events: _live_match(m, event),
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            event_bus=event_bus,
            enabled=True,
            source="sports_live_aggregate",
            leagues=("nba",),
            publish_entry_signals=False,
        )
        await worker.sync_once()
        return {
            "audit_event": await event_bus.next_persistence_event(),
        }

    audit_event = asyncio.run(run())["audit_event"]
    assert audit_event.event_type == DomainEventType.SPORTS_LIVE_STATE_RECORDED
    payload = audit_event.payload
    assert payload["primary_source"] == "nba"
    assert payload["contributing_sources"] == ["nba", "espn"]
    assert payload["confidence"] == 0.7
    conflicts = payload["source_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["field"] == "status"
    assert conflicts[0]["winner_source"] == "nba"
    assert conflicts[0]["loser_source"] == "espn"


def test_entry_signal_event_includes_primary_and_contributing_sources() -> None:
    """entry signal 也要带源选择信息，让 trading decision 路径能 audit 这些字段。"""

    async def run() -> dict[str, object]:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        event = _event_with_conflicts()
        event_bus = EventBus(trading_capacity=10, maintenance_capacity=10, persistence_capacity=10)

        async def provider() -> SportsLiveSnapshot:
            return SportsLiveSnapshot(source="sports_live_aggregate", observed_at=_OBSERVED, events=(event,))

        worker = SportsLiveStateWorker(
            snapshot_provider=provider,
            match_live_state=lambda m, _events: _live_match(m, event),
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            event_bus=event_bus,
            enabled=True,
            source="sports_live_aggregate",
            leagues=("nba",),
            publish_entry_signals=True,
        )
        await worker.sync_once()
        return {
            "entry_event": await event_bus.next_trading_event(),
        }

    entry_event = asyncio.run(run())["entry_event"]
    assert entry_event.event_type == DomainEventType.ENTRY_SIGNAL_TRIGGERED
    payload = entry_event.payload
    assert payload["primary_source"] == "nba"
    assert payload["contributing_sources"] == ["nba", "espn"]
    assert payload["confidence"] == 0.7


def test_recent_match_sources_ring_buffer_exposed_to_admin() -> None:
    """worker.recent_match_sources 是 admin 缺口 1 入口：返回 per-market 条目列表。"""

    async def run() -> tuple[dict[str, object], ...]:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        event = _event_with_conflicts()

        async def provider() -> SportsLiveSnapshot:
            return SportsLiveSnapshot(source="sports_live_aggregate", observed_at=_OBSERVED, events=(event,))

        worker = SportsLiveStateWorker(
            snapshot_provider=provider,
            match_live_state=lambda m, _events: _live_match(m, event),
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            enabled=True,
            source="sports_live_aggregate",
            leagues=("nba",),
            publish_entry_signals=False,
        )
        await worker.sync_once()
        return worker.recent_match_sources()

    items = asyncio.run(run())
    assert len(items) == 1
    item = items[0]
    assert item["condition_id"] == "moneyline-condition"
    assert item["primary_source"] == "nba"
    assert item["contributing_sources"] == ["nba", "espn"]
    assert item["confidence"] == 0.7
    assert item["conflict_count"] == 1
