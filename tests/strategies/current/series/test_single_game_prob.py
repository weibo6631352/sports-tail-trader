"""单场胜率派生：game_odds 优先 / Pythagorean fallback / 缺失返回 None。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.sports_season import (
    SeasonSnapshot,
    SeasonStandingRow,
    SeasonStandings,
)
from strategies.current.series.single_game_prob import derive_single_game_prob
from strategies.current.series.types import SeriesState


_NOW = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)


def _state() -> SeriesState:
    return SeriesState(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        wins_a=2,
        wins_b=1,
        best_of=7,
        next_game_at=None,
        observed_at=_NOW,
    )


def _game_odds_meta(team_a: str = "Boston Celtics", team_b: str = "New York Knicks", p_a: str = "0.62") -> dict:
    return {
        "game_odds": {
            "team_a": team_a,
            "team_b": team_b,
            "p_a": p_a,
            "observed_at": _NOW.isoformat(),
            "source": "theoddsapi",
        }
    }


def test_game_odds_takes_priority() -> None:
    derived = derive_single_game_prob(
        state=_state(),
        metadata=_game_odds_meta(),
        season_snapshot=None,
        now=_NOW,
        max_game_odds_age_seconds=7200,
    )
    assert derived is not None
    assert derived.p_a == Decimal("0.62")
    assert derived.source.startswith("game_odds")


def test_game_odds_inverted_team_order_returns_complement() -> None:
    # game_odds team order swapped (team_a=Knicks)
    meta = _game_odds_meta(team_a="New York Knicks", team_b="Boston Celtics", p_a="0.62")
    derived = derive_single_game_prob(
        state=_state(),
        metadata=meta,
        season_snapshot=None,
        now=_NOW,
        max_game_odds_age_seconds=7200,
    )
    assert derived is not None
    assert derived.p_a == Decimal("0.38")


def test_stale_game_odds_falls_through_to_pythagorean() -> None:
    stale_meta = {
        "game_odds": {
            "team_a": "Boston Celtics",
            "team_b": "New York Knicks",
            "p_a": "0.62",
            # 24h 前
            "observed_at": (_NOW.replace(day=12)).isoformat(),
            "source": "theoddsapi",
        }
    }
    standings = SeasonStandings(
        league="NBA",
        season_id="2026",
        observed_at=_NOW,
        rows=(
            SeasonStandingRow(team="Boston Celtics", wins=55, losses=27, win_pct=Decimal("0.671")),
            SeasonStandingRow(team="New York Knicks", wins=45, losses=37, win_pct=Decimal("0.549")),
        ),
        source="espn",
    )
    snapshot = SeasonSnapshot(observed_at=_NOW, standings=(standings,), source="espn")
    derived = derive_single_game_prob(
        state=_state(),
        metadata=stale_meta,
        season_snapshot=snapshot,
        now=_NOW,
        max_game_odds_age_seconds=3600,  # 1h — stale fails
    )
    assert derived is not None
    # 0.671 / (0.671 + 0.549) ≈ 0.5500
    assert abs(derived.p_a - Decimal("0.55")) < Decimal("0.01")
    assert derived.source == "pythagorean"


def test_pythagorean_fallback_when_no_game_odds() -> None:
    standings = SeasonStandings(
        league="NBA",
        season_id="2026",
        observed_at=_NOW,
        rows=(
            SeasonStandingRow(team="Boston Celtics", wins=60, losses=22, win_pct=Decimal("0.732")),
            SeasonStandingRow(team="New York Knicks", wins=40, losses=42, win_pct=Decimal("0.488")),
        ),
        source="espn",
    )
    snapshot = SeasonSnapshot(observed_at=_NOW, standings=(standings,), source="espn")
    derived = derive_single_game_prob(
        state=_state(),
        metadata={},
        season_snapshot=snapshot,
        now=_NOW,
        max_game_odds_age_seconds=3600,
    )
    assert derived is not None
    assert derived.source == "pythagorean"


def test_no_sources_returns_none() -> None:
    derived = derive_single_game_prob(
        state=_state(),
        metadata={},
        season_snapshot=None,
        now=_NOW,
        max_game_odds_age_seconds=3600,
    )
    assert derived is None


def test_pythagorean_returns_none_when_team_missing_from_standings() -> None:
    standings = SeasonStandings(
        league="NBA",
        season_id="2026",
        observed_at=_NOW,
        rows=(
            SeasonStandingRow(team="Los Angeles Lakers", wins=50, losses=32, win_pct=Decimal("0.610")),
        ),
        source="espn",
    )
    snapshot = SeasonSnapshot(observed_at=_NOW, standings=(standings,), source="espn")
    derived = derive_single_game_prob(
        state=_state(),
        metadata={},
        season_snapshot=snapshot,
        now=_NOW,
        max_game_odds_age_seconds=3600,
    )
    assert derived is None
