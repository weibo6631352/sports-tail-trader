from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polymarket_trader.domain.sports_season import (
    SeasonSnapshot,
    SeasonStandingRow,
    SeasonStandings,
    SeriesScore,
)
from polymarket_trader.storage.season_state_store import SeasonStateStore
from polymarket_trader.workers.sports_season_state_worker import (
    SportsSeasonStateWorker,
)


def _snapshot(observed_at: datetime) -> SeasonSnapshot:
    return SeasonSnapshot(
        observed_at=observed_at,
        standings=(
            SeasonStandings(
                league="NBA",
                season_id="2026",
                observed_at=observed_at,
                rows=(SeasonStandingRow(team="Celtics", wins=58),),
                source="espn",
            ),
        ),
        series=(
            SeriesScore(
                league="NBA",
                series_id="bos-nyk-east-finals",
                home_team="Celtics",
                away_team="Knicks",
                home_wins=3,
                away_wins=1,
                best_of=7,
                observed_at=observed_at,
                source="espn",
            ),
        ),
        source="espn",
    )


def test_worker_disabled_returns_none() -> None:
    store = SeasonStateStore()
    fixed_now = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)

    async def provider() -> SeasonSnapshot:
        return _snapshot(fixed_now)

    worker = SportsSeasonStateWorker(
        snapshot_provider=provider,
        store=store,
    )
    result = asyncio.run(worker.sync_once())
    assert result is None
    assert store.all_standings() == ()


def test_worker_persists_standings_and_series() -> None:
    store = SeasonStateStore()
    observed_at = datetime(2026, 5, 11, tzinfo=timezone.utc)

    async def provider() -> SeasonSnapshot:
        return _snapshot(observed_at)

    worker = SportsSeasonStateWorker(
        snapshot_provider=provider,
        store=store,
        enabled=True,
    )
    snapshot = asyncio.run(worker.sync_once())
    assert snapshot is not None
    assert len(store.all_standings()) == 1
    assert store.get_standings("NBA", "2026") is not None
    assert store.get_series("NBA", "bos-nyk-east-finals") is not None

    status = worker.status_snapshot()
    assert status.last_standings_count == 1
    assert status.last_series_count == 1
    assert status.consecutive_failures == 0


def test_worker_records_failure_and_propagates_exception() -> None:
    store = SeasonStateStore()

    async def failing_provider() -> SeasonSnapshot:
        raise RuntimeError("api down")

    worker = SportsSeasonStateWorker(
        snapshot_provider=failing_provider,
        store=store,
        enabled=True,
    )
    try:
        asyncio.run(worker.sync_once())
    except RuntimeError as exc:
        assert "api down" in str(exc)
    status = worker.status_snapshot()
    assert status.consecutive_failures == 1
    assert status.last_error == "api down"
