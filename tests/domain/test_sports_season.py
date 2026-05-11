from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.sports_season import (
    SeasonOddsSnapshot,
    SeasonSnapshot,
    SeasonStandingRow,
    SeasonStandings,
    SeriesScore,
)


def test_season_standings_holds_typed_rows() -> None:
    rows = (
        SeasonStandingRow(team="Celtics", wins=58, losses=24, win_pct=Decimal("0.707"), seed=1),
        SeasonStandingRow(team="Knicks", wins=50, losses=32, win_pct=Decimal("0.610")),
    )
    standings = SeasonStandings(
        league="NBA",
        season_id="2026",
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        rows=rows,
        source="espn",
    )
    assert standings.rows[0].team == "Celtics"
    assert standings.rows[0].wins == 58
    assert standings.rows[0].seed == 1
    assert standings.rows[1].seed is None


def test_season_odds_snapshot_lookup_handles_case() -> None:
    snapshot = SeasonOddsSnapshot(
        market_key="nba-championship-2026",
        fair_probabilities={
            "Boston Celtics": Decimal("0.45"),
            "denver nuggets": Decimal("0.20"),
        },
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        source="theoddsapi",
    )
    assert snapshot.probability_for("Boston Celtics") == Decimal("0.45")
    assert snapshot.probability_for("Denver Nuggets") == Decimal("0.20")
    assert snapshot.probability_for("Phoenix Suns") is None
    assert snapshot.probability_for("") is None


def test_season_snapshot_aggregates_standings_and_series() -> None:
    standings = SeasonStandings(
        league="NBA",
        season_id="2026",
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        rows=(),
    )
    series = SeriesScore(
        league="NBA",
        series_id="bos-vs-nyk-2026-east-finals",
        home_team="Celtics",
        away_team="Knicks",
        home_wins=2,
        away_wins=1,
        best_of=7,
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    snapshot = SeasonSnapshot(
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        standings=(standings,),
        series=(series,),
        source="espn",
    )
    assert len(snapshot.standings) == 1
    assert snapshot.series[0].home_wins == 2
    payload = snapshot.as_dict()
    assert payload["source"] == "espn"
    assert len(payload["standings"]) == 1
