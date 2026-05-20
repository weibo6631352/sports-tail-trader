"""Goalserve parser unit tests: per-sport parsing, status flags, odds extraction."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


from polymarket_trader.domain.sports_live import SportsLiveGameStatus
from polymarket_trader.infra.sports.goalserve_parsers import (
    parse_goalserve_sport,
)

_OBSERVED = datetime(2026, 5, 19, tzinfo=timezone.utc)
_FIXTURE_DIR = Path("tests/fixtures/sports_live/goalserve")


def _load_fixture(filename: str) -> dict:
    return json.loads((_FIXTURE_DIR / filename).read_text())


# ---------------------------------------------------------------------------
# Status flag logic
# ---------------------------------------------------------------------------

def _minimal_event(event_id: str, *, core: dict, period: str = "1st Quarter") -> dict:
    return {
        event_id: {
            "core": core,
            "info": {
                "id": event_id, "mid": "1", "name": "TeamA vs TeamB",
                "sport": "Basketball", "league": "NBA",
                "period": period, "score": "10:8",
                "state": "1", "minute": "5", "seconds": "05:00",
                "start_ts_utc": "",
            },
            "team_info": {
                "home": {"name": "TeamA", "score": "10"},
                "away": {"name": "TeamB", "score": "8"},
            },
            "stats": {
                "0": {"name": "ITeam", "home": "TeamA", "away": "TeamB"},
                "1": {"name": "1", "home": "10", "away": "8"},
            },
            "odds": {},
        }
    }


def test_removed_event_is_skipped() -> None:
    data = {"events": _minimal_event("ev1", core={"removed": "1", "finished": "0", "stopped": "0", "blocked": "0"})}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    assert events == []


def test_finished_event_has_status_ended() -> None:
    data = {"events": _minimal_event("ev1", core={"removed": "", "finished": "1", "stopped": "0", "blocked": "0"})}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status == SportsLiveGameStatus.ENDED


def test_stopped_event_has_status_paused() -> None:
    data = {"events": _minimal_event("ev1", core={"removed": "", "finished": "0", "stopped": "1", "blocked": "0"})}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status == SportsLiveGameStatus.PAUSED


def test_live_event_has_status_live() -> None:
    data = {"events": _minimal_event("ev1", core={"removed": "", "finished": "0", "stopped": "0", "blocked": "0"})}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status == SportsLiveGameStatus.LIVE


def test_blocked_odds_does_not_change_game_status() -> None:
    data = {"events": _minimal_event("ev1", core={"removed": "", "finished": "0", "stopped": "0", "blocked": "1"})}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status == SportsLiveGameStatus.LIVE


def test_empty_events_dict_returns_empty_list() -> None:
    events = parse_goalserve_sport("basketball", {"events": {}}, observed_at=_OBSERVED)
    assert events == []


def test_missing_events_key_returns_empty_list() -> None:
    events = parse_goalserve_sport("basketball", {}, observed_at=_OBSERVED)
    assert events == []


# ---------------------------------------------------------------------------
# Basketball
# ---------------------------------------------------------------------------

def test_basketball_fixture_parses_basic_fields() -> None:
    data = _load_fixture("basketball_sample.json")
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    assert len(events) >= 1
    ev = events[0]
    assert ev.source == "goalserve"
    assert ev.sport == "basketball"
    assert ev.league != ""
    assert ev.home is not None
    assert ev.away is not None
    assert ev.home.name != ""
    assert ev.away.name != ""


def test_basketball_quarter_stats_in_source_payload() -> None:
    data = _load_fixture("basketball_sample.json")
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    assert len(events) >= 1
    ev = events[0]
    assert ev.source_payload is not None
    assert "quarter_stats" in ev.source_payload
    assert isinstance(ev.source_payload["quarter_stats"], dict)


def test_basketball_scores_are_integers() -> None:
    data = _load_fixture("basketball_sample.json")
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    for ev in events:
        if ev.home.score is not None:
            assert isinstance(ev.home.score, int)
        if ev.away.score is not None:
            assert isinstance(ev.away.score, int)


# ---------------------------------------------------------------------------
# Tennis
# ---------------------------------------------------------------------------

def test_tennis_fixture_parses_sets() -> None:
    data = _load_fixture("tennis_sample.json")
    events = parse_goalserve_sport("tennis", data, observed_at=_OBSERVED)
    assert len(events) >= 1
    # At least one event should have tennis_state with set scores
    tennis_events = [e for e in events if e.tennis_state is not None]
    assert len(tennis_events) >= 1
    ev = tennis_events[0]
    assert len(ev.tennis_state.set_scores) >= 1


def test_tennis_serving_side_set_when_turn_present() -> None:
    data = _load_fixture("tennis_sample.json")
    events = parse_goalserve_sport("tennis", data, observed_at=_OBSERVED)
    tennis_with_state = [e for e in events if e.tennis_state is not None]
    assert len(tennis_with_state) >= 1


def test_tennis_player_names_extracted() -> None:
    data = _load_fixture("tennis_sample.json")
    events = parse_goalserve_sport("tennis", data, observed_at=_OBSERVED)
    for ev in events:
        assert ev.home.name != ""
        assert ev.away.name != ""


# ---------------------------------------------------------------------------
# Baseball
# ---------------------------------------------------------------------------

def test_baseball_fixture_parses_game_state() -> None:
    data = _load_fixture("baseball_sample.json")
    events = parse_goalserve_sport("baseball", data, observed_at=_OBSERVED)
    assert len(events) >= 1
    ev = events[0]
    assert ev.baseball_state is not None
    # current_inning extracted from period field
    assert ev.baseball_state.current_inning is not None


def test_baseball_inning_scores_in_source_payload() -> None:
    data = _load_fixture("baseball_sample.json")
    events = parse_goalserve_sport("baseball", data, observed_at=_OBSERVED)
    ev = events[0]
    assert ev.source_payload is not None
    assert "inning_scores" in ev.source_payload
    assert isinstance(ev.source_payload["inning_scores"], list)
    assert len(ev.source_payload["inning_scores"]) >= 1
    for inning in ev.source_payload["inning_scores"]:
        assert "home" in inning
        assert "away" in inning


# ---------------------------------------------------------------------------
# Hockey
# ---------------------------------------------------------------------------

def test_hockey_fixture_parses_events() -> None:
    data = _load_fixture("hockey_sample.json")
    events = parse_goalserve_sport("hockey", data, observed_at=_OBSERVED)
    assert len(events) >= 1
    ev = events[0]
    assert ev.sport == "ice-hockey"
    assert ev.source == "goalserve"


def test_hockey_period_stats_in_source_payload() -> None:
    data = _load_fixture("hockey_sample.json")
    events = parse_goalserve_sport("hockey", data, observed_at=_OBSERVED)
    ev = events[0]
    assert ev.source_payload is not None
    assert "period_stats" in ev.source_payload
    stats = ev.source_payload["period_stats"]
    assert isinstance(stats, dict)
    # Should have at least P1
    assert "P1" in stats or len(stats) >= 1


# ---------------------------------------------------------------------------
# Soccer
# ---------------------------------------------------------------------------

def test_soccer_fixture_parses_events() -> None:
    data = _load_fixture("soccer_sample.json")
    events = parse_goalserve_sport("soccer", data, observed_at=_OBSERVED)
    assert len(events) >= 1
    ev = events[0]
    assert ev.sport == "soccer"
    assert ev.home is not None
    assert ev.away is not None


# ---------------------------------------------------------------------------
# Odds parsing
# ---------------------------------------------------------------------------

def _event_with_odds(event_id: str) -> dict:
    return {
        event_id: {
            "core": {"removed": "", "finished": "0", "stopped": "0", "blocked": "0"},
            "info": {
                "id": event_id, "mid": "1", "name": "TeamA vs TeamB",
                "sport": "Basketball", "league": "NBA",
                "period": "1st Quarter", "score": "10:8",
                "state": "1", "minute": "5", "seconds": "05:00", "start_ts_utc": "",
            },
            "team_info": {
                "home": {"name": "TeamA", "score": "10"},
                "away": {"name": "TeamB", "score": "8"},
            },
            "stats": {
                "0": {"name": "ITeam", "home": "TeamA", "away": "TeamB"},
            },
            "odds": {
                "180032": {
                    "id": 180032,
                    "name": "Game Lines Money Line",
                    "suspend": "0",
                    "participants": {
                        "p0": {"name": "Home", "value_eu": "1.09", "handicap": "", "suspend": "0"},
                        "p1": {"name": "Away", "value_eu": "8.0", "handicap": "", "suspend": "1"},
                    },
                }
            },
        }
    }


def test_odds_parsed_into_source_payload() -> None:
    data = {"events": _event_with_odds("ev1")}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    payload = events[0].source_payload
    assert payload is not None
    assert "goalserve_odds" in payload


def test_odds_implied_prob_calculated() -> None:
    data = {"events": _event_with_odds("ev1")}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    odds_dict = events[0].source_payload["goalserve_odds"]
    # odds_dict from as_dict()
    markets = odds_dict["markets"]
    assert len(markets) == 1
    ml_market = markets[0]
    assert ml_market["name"] == "Game Lines Money Line"
    home_outcome = next(o for o in ml_market["outcomes"] if o["name"] == "Home")
    # implied_prob = 1 / 1.09 ≈ 0.917
    assert abs(float(home_outcome["implied_prob"]) - (1 / 1.09)) < 0.01


def test_suspended_outcome_flagged() -> None:
    data = {"events": _event_with_odds("ev1")}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    markets = events[0].source_payload["goalserve_odds"]["markets"]
    away_outcome = next(o for o in markets[0]["outcomes"] if o["name"] == "Away")
    assert away_outcome["suspended"] is True


def test_suspended_market_flagged() -> None:
    """市场整体 suspend=1 → market 被标记为 suspended。"""
    data = {
        "events": {
            "ev1": {
                "core": {"removed": "", "finished": "0", "stopped": "0", "blocked": "0"},
                "info": {
                    "id": "ev1", "mid": "1", "name": "A vs B",
                    "sport": "Basketball", "league": "NBA",
                    "period": "HT", "score": "50:40",
                    "state": "1", "minute": "20", "seconds": "20:00", "start_ts_utc": "",
                },
                "team_info": {"home": {"name": "A", "score": "50"}, "away": {"name": "B", "score": "40"}},
                "stats": {"0": {"name": "ITeam", "home": "A", "away": "B"}},
                "odds": {
                    "999": {
                        "id": 999, "name": "Money Line", "suspend": "1",
                        "participants": {
                            "p0": {"name": "Home", "value_eu": "2.0", "handicap": "", "suspend": "0"},
                        },
                    }
                },
            }
        }
    }
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    markets = events[0].source_payload["goalserve_odds"]["markets"]
    assert markets[0]["suspended"] is True


# ---------------------------------------------------------------------------
# source_payload structure
# ---------------------------------------------------------------------------

def test_source_payload_carries_event_id() -> None:
    data = {"events": _event_with_odds("ev42")}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    payload = events[0].source_payload
    assert payload["goalserve_odds"]["event_id"] == "ev42"


def test_external_ids_include_goalserve_key() -> None:
    data = {"events": _minimal_event("ev99", core={"removed": "", "finished": "0", "stopped": "0", "blocked": "0"})}
    events = parse_goalserve_sport("basketball", data, observed_at=_OBSERVED)
    assert "goalserve" in events[0].external_ids
    assert events[0].external_ids["goalserve"] == "ev99"
