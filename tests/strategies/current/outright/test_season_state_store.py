from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trader.domain.sports_season import (
    SeasonStandingRow,
    SeasonStandings,
    SeriesScore,
)
from polymarket_trader.storage.season_state_store import SeasonStateStore


def _standings(observed_at: datetime, team: str = "Celtics") -> SeasonStandings:
    return SeasonStandings(
        league="NBA",
        season_id="2026",
        observed_at=observed_at,
        rows=(SeasonStandingRow(team=team, wins=50, losses=20),),
    )


def test_store_upsert_and_get_round_trips() -> None:
    store = SeasonStateStore()
    standings = _standings(datetime(2026, 5, 11, tzinfo=timezone.utc))
    store.upsert_standings(standings)
    fetched = store.get_standings("NBA", "2026")
    assert fetched is not None
    assert fetched.rows[0].team == "Celtics"


def test_store_does_not_overwrite_newer_with_older() -> None:
    store = SeasonStateStore()
    newer = _standings(datetime(2026, 5, 11, 12, tzinfo=timezone.utc), team="Knicks")
    older = _standings(datetime(2026, 5, 10, 12, tzinfo=timezone.utc), team="Celtics")
    store.upsert_standings(newer)
    store.upsert_standings(older)
    fetched = store.get_standings("NBA", "2026")
    assert fetched is not None
    assert fetched.rows[0].team == "Knicks"


def test_store_series_upsert_isolated_from_standings() -> None:
    store = SeasonStateStore()
    series = SeriesScore(
        league="NBA",
        series_id="bos-vs-nyk-east-finals",
        home_team="Celtics",
        away_team="Knicks",
        home_wins=2,
        away_wins=1,
        best_of=7,
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    store.upsert_series(series)
    assert store.get_series("NBA", "bos-vs-nyk-east-finals") is series
    assert store.get_standings("NBA", "2026") is None


def test_store_standings_by_league_returns_latest_per_league() -> None:
    store = SeasonStateStore()
    earlier = _standings(datetime(2026, 5, 1, tzinfo=timezone.utc))
    later = SeasonStandings(
        league="NBA",
        season_id="2027",
        observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        rows=(SeasonStandingRow(team="Lakers"),),
    )
    store.upsert_standings(earlier)
    store.upsert_standings(later)
    grouped = store.standings_by_league()
    assert grouped["nba"].season_id == "2027"


def test_store_iter_yields_all_standings() -> None:
    store = SeasonStateStore()
    base = datetime(2026, 5, 11, tzinfo=timezone.utc)
    store.upsert_standings(_standings(base))
    store.upsert_standings(
        SeasonStandings(
            league="NHL",
            season_id="2026",
            observed_at=base + timedelta(minutes=1),
            rows=(SeasonStandingRow(team="Rangers"),),
        )
    )
    iter_leagues = sorted(s.league for s in store)
    assert iter_leagues == ["NBA", "NHL"]
