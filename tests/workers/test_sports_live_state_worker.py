from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveSourceStatus,
    SportsLiveTeam,
)
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.sports_live_state_worker import SportsLiveStateWorker
from strategies.current.live_state import sports_live_metadata_match


def test_sports_live_state_worker_writes_metadata_and_entry_signals() -> None:
    result = asyncio.run(_run_sync_with_match())

    metadata = result["metadata"]
    status = result["status"]
    first_event = result["first_event"]
    second_event = result["second_event"]

    assert metadata["sports_tail_game"]["league"] == "NBA"
    assert metadata["sports_tail_game"]["home_score"] == 102
    assert metadata["sports_tail_game"]["away_score"] == 94
    assert metadata["sports_tail_game"]["seconds_remaining"] == 90
    assert metadata["sports_live_match"]["source_event_id"] == "game-1"
    assert status.last_games_seen == 1
    assert status.last_matches == 1
    assert status.last_records_written == 1
    assert status.last_entry_signals_published == 2
    assert result["last_games"][0].source_event_id == "game-1"
    assert first_event.event_type == DomainEventType.ENTRY_SIGNAL_TRIGGERED
    assert second_event.event_type == DomainEventType.ENTRY_SIGNAL_TRIGGERED
    assert {first_event.token_id, second_event.token_id} == {"home", "away"}


def test_sports_live_state_worker_tracks_entry_signal_market_for_market_ws() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        tracker = _MarketTracker()
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=sports_live_metadata_match,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            market_tracker=tracker,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        sync_result = await worker.sync_once()

        assert sync_result is not None
        assert sync_result.records_written == 1
        assert tracker.tracked_condition_ids == ["moneyline-condition"]

    asyncio.run(run())


def test_sports_live_state_worker_does_not_track_blocked_entry_signal_market() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        registry.upsert(_market())
        tracker = _MarketTracker()
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=lambda market, games: (
                market,
                games[0],
                {
                    "sports_tail_game": {
                        "league": "NBA",
                        "home_name": "Knicks",
                        "away_name": "Celtics",
                        "home_score": 102,
                        "away_score": 94,
                        "period": "Q4",
                        "seconds_remaining": 90,
                        "status": "live",
                    },
                    "sports_tail_entry_signal_allowed": False,
                    "sports_tail_entry_signal_reason": "market_end_too_far",
                },
            ),
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            market_tracker=tracker,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        sync_result = await worker.sync_once()

        assert sync_result is not None
        assert sync_result.records_written == 1
        assert tracker.tracked_condition_ids == []

    asyncio.run(run())


def test_sports_live_state_worker_keeps_unmatched_markets_auditable() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        registry.upsert(_market())
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(
                _game(
                    home="Lakers",
                    away="Heat",
                    home_display="Los Angeles Lakers",
                    away_display="Miami Heat",
                    home_abbreviation="LAL",
                    away_abbreviation="MIA",
                )
            ),
            match_live_state=sports_live_metadata_match,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        sync_result = await worker.sync_once()
        status = worker.status_snapshot()

        assert sync_result is not None
        assert sync_result.matches == 0
        assert status.last_unmatched_markets == 1
        assert status.last_records_written == 0
        assert status.last_entry_signals_published == 0

    asyncio.run(run())


def test_sports_live_state_worker_writes_metadata_without_blocked_entry_signals() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        registry.upsert(_market())
        store = EntryMetadataStore()
        event_bus = EventBus(trading_capacity=10, maintenance_capacity=10, persistence_capacity=10)
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=lambda market, games: (
                market,
                games[0],
                {
                    "sports_tail_game": {
                        "league": "NBA",
                        "home_name": "Knicks",
                        "away_name": "Celtics",
                        "home_score": 102,
                        "away_score": 94,
                        "period": "Q4",
                        "seconds_remaining": 90,
                        "status": "live",
                    },
                    "sports_tail_entry_signal_allowed": False,
                    "sports_tail_entry_signal_reason": "market_end_too_far",
                },
            ),
            registry=registry,
            entry_metadata_store=store,
            event_bus=event_bus,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=True,
        )

        sync_result = await worker.sync_once()

        assert sync_result is not None
        assert sync_result.records_written == 1
        assert sync_result.entry_signals_published == 0
        assert event_bus.trading_queue_depth() == 0
        assert store.metadata_for(condition_id="moneyline-condition")["sports_tail_entry_signal_allowed"] is False

    asyncio.run(run())


def test_sports_live_state_worker_exposes_source_statuses() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        registry.upsert(_market())
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(
                _game(),
                source="sports_live_aggregate",
                source_statuses=(
                    SportsLiveSourceStatus(source="espn", success=True, games_seen=1),
                    SportsLiveSourceStatus(source="nba", success=False, last_error="timeout"),
                ),
            ),
            match_live_state=sports_live_metadata_match,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            enabled=True,
            source="sports_live_aggregate",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        sync_result = await worker.sync_once()
        status = worker.status_snapshot()

        assert sync_result is not None
        assert sync_result.source_statuses[0].source == "espn"
        assert status.source_statuses[1].source == "nba"
        assert status.source_statuses[1].last_error == "timeout"

    asyncio.run(run())


def test_sports_live_state_worker_yields_during_bulk_market_matching() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        for index in range(25):
            registry.upsert(_market(condition_id=f"condition-{index}", slug=f"nba-nyk-bos-{index}"))
        sync_done = False
        yielded_before_sync_done = False

        async def observer() -> None:
            nonlocal yielded_before_sync_done
            await asyncio.sleep(0)
            yielded_before_sync_done = not sync_done

        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=lambda _market, _games: None,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        observer_task = asyncio.create_task(observer())
        await worker.sync_once()
        sync_done = True
        await observer_task

        assert yielded_before_sync_done is True

    asyncio.run(run())


async def _run_sync_with_match() -> dict[str, object]:
    registry = MarketRegistry()
    registry.upsert(_market())
    store = EntryMetadataStore()
    event_bus = EventBus(trading_capacity=10, maintenance_capacity=10, persistence_capacity=10)
    worker = SportsLiveStateWorker(
        snapshot_provider=lambda: _snapshot(_game()),
        match_live_state=sports_live_metadata_match,
        registry=registry,
        entry_metadata_store=store,
        event_bus=event_bus,
        enabled=True,
        source="espn",
        leagues=("nba",),
        publish_entry_signals=True,
    )

    sync_result = await worker.sync_once()
    assert sync_result is not None
    return {
        "metadata": store.metadata_for(condition_id="moneyline-condition"),
        "status": worker.status_snapshot(),
        "last_games": worker.last_games(),
        "first_event": await event_bus.next_trading_event(),
        "second_event": await event_bus.next_trading_event(),
    }


async def _snapshot(
    game: SportsLiveGame,
    *,
    source: str = "espn",
    source_statuses: tuple[SportsLiveSourceStatus, ...] = (),
) -> SportsLiveSnapshot:
    return SportsLiveSnapshot(
        source=source,
        observed_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        games=(game,),
        source_statuses=source_statuses,
    )


def _game(
    *,
    home: str = "Knicks",
    away: str = "Celtics",
    home_display: str | None = None,
    away_display: str | None = None,
    home_abbreviation: str = "NYK",
    away_abbreviation: str = "BOS",
) -> SportsLiveGame:
    return SportsLiveGame(
        source="espn",
        source_event_id="game-1",
        league="NBA",
        home=SportsLiveTeam(
            name=home,
            score=102,
            display_name=home_display or f"New York {home}",
            abbreviation=home_abbreviation,
            short_name=home,
        ),
        away=SportsLiveTeam(
            name=away,
            score=94,
            display_name=away_display or f"Boston {away}",
            abbreviation=away_abbreviation,
            short_name=away,
        ),
        status=SportsLiveGameStatus.LIVE,
        period="Q4",
        seconds_remaining=90,
        observed_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        raw_status="STATUS_IN_PROGRESS",
    )


def _market(
    *,
    condition_id: str = "moneyline-condition",
    slug: str = "nba-nyk-bos-moneyline",
) -> Market:
    return Market(
        condition_id=condition_id,
        market_slug=slug,
        market_question="New York Knicks vs Boston Celtics moneyline",
        event_title="New York Knicks vs Boston Celtics",
        event_slug="new-york-knicks-vs-boston-celtics",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="home", outcome="NYK"),
            MarketOutcome(token_id="away", outcome="BOS"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


class _MarketTracker:
    def __init__(self) -> None:
        self.tracked_condition_ids: list[str] = []

    def track_market(self, market: Market) -> None:
        self.tracked_condition_ids.append(market.condition_id)
