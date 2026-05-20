"""Goalserve WS parser unit tests: per-sport parsing, status mapping, odds extraction."""
from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.sports_live import SportsLiveGameStatus
from polymarket_trader.infra.sports.goalserve_parsers import (
    parse_goalserve_ws_events,
)

_OBSERVED = datetime(2026, 5, 19, tzinfo=timezone.utc)


def _ws_event(
    event_id: str,
    *,
    sport: str = "soccer",
    stp: int = 1,
    home: str = "TeamA",
    away: str = "TeamB",
    home_score: int = 1,
    away_score: int = 0,
    et: int = 2000,
    st: int = 1779296400,
    league: str = "Test League",
    odds: list | None = None,
    sc: str = "11001",
) -> dict:
    return {
        "mt": "updt",
        "sp": sport,
        "id": event_id,
        "t1": {"n": home},
        "t2": {"n": away},
        "stp": stp,
        "et": et,
        "sc": sc,
        "ctry_name": league,
        "st": st,
        "stats": {"g": [home_score, away_score], "y": [0, 1], "r": [0, 0], "c": [2, 3]},
        "odds": odds or [],
    }


def _state(event_id: str, **kwargs) -> dict:
    return {event_id: _ws_event(event_id, **kwargs)}


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

def test_stp_1_is_live() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", stp=1), observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status == SportsLiveGameStatus.LIVE


def test_stp_3_is_ended() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", stp=3), observed_at=_OBSERVED)
    assert events[0].status == SportsLiveGameStatus.ENDED


def test_stp_7_is_paused() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", stp=7), observed_at=_OBSERVED)
    assert events[0].status == SportsLiveGameStatus.PAUSED


def test_stp_0_is_scheduled() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", stp=0), observed_at=_OBSERVED)
    assert events[0].status == SportsLiveGameStatus.SCHEDULED


def test_stp_4_is_postponed() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", stp=4), observed_at=_OBSERVED)
    assert events[0].status == SportsLiveGameStatus.POSTPONED


def test_stp_5_is_cancelled() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", stp=5), observed_at=_OBSERVED)
    assert events[0].status == SportsLiveGameStatus.CANCELLED


def test_empty_state_returns_empty_list() -> None:
    events = parse_goalserve_ws_events("soccer", {}, observed_at=_OBSERVED)
    assert events == []


def test_unknown_sport_returns_empty_list() -> None:
    events = parse_goalserve_ws_events("curling", _state("ev1"), observed_at=_OBSERVED)
    assert events == []


# ---------------------------------------------------------------------------
# Soccer
# ---------------------------------------------------------------------------

def test_soccer_basic_fields() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", home="Benfica", away="Porto", home_score=2, away_score=1), observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.source == "goalserve"
    assert ev.sport == "soccer"
    assert ev.home.name == "Benfica"
    assert ev.away.name == "Porto"
    assert ev.home.score == 2
    assert ev.away.score == 1
    assert ev.league == "Test League"


def test_soccer_state_period_first_half() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", et=1800), observed_at=_OBSERVED)
    assert events[0].soccer_state is not None
    assert events[0].soccer_state.period == "first_half"
    assert events[0].soccer_state.clock_minutes == 30


def test_soccer_state_period_second_half() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", et=4000), observed_at=_OBSERVED)
    assert events[0].soccer_state.period == "second_half"


def test_soccer_card_counts() -> None:
    state = {
        "ev1": {
            **_ws_event("ev1"),
            "stats": {"g": [0, 0], "y": [1, 2], "r": [0, 1], "c": [3, 4]},
        }
    }
    events = parse_goalserve_ws_events("soccer", state, observed_at=_OBSERVED)
    ss = events[0].soccer_state
    assert ss.home_yellow_cards == 1
    assert ss.away_yellow_cards == 2
    assert ss.home_red_cards == 0
    assert ss.away_red_cards == 1


def test_soccer_start_time_parsed() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", st=1779296400), observed_at=_OBSERVED)
    assert events[0].event_start_time is not None
    assert events[0].event_start_time.year >= 2026


# ---------------------------------------------------------------------------
# Basketball
# ---------------------------------------------------------------------------

def test_basketball_basic_fields() -> None:
    events = parse_goalserve_ws_events(
        "basketball",
        _state("ev1", sport="basketball", home="Lakers", away="Celtics", home_score=105, away_score=98),
        observed_at=_OBSERVED,
    )
    ev = events[0]
    assert ev.sport == "basketball"
    assert ev.home.name == "Lakers"
    assert ev.away.score == 98


def test_basketball_scores_are_integers() -> None:
    events = parse_goalserve_ws_events(
        "basketball",
        _state("ev1", sport="basketball", home_score=87, away_score=92),
        observed_at=_OBSERVED,
    )
    assert isinstance(events[0].home.score, int)
    assert isinstance(events[0].away.score, int)


# ---------------------------------------------------------------------------
# Hockey
# ---------------------------------------------------------------------------

def test_hockey_sport_label() -> None:
    events = parse_goalserve_ws_events(
        "hockey",
        _state("ev1", sport="hockey", home_score=3, away_score=2),
        observed_at=_OBSERVED,
    )
    assert events[0].sport == "ice-hockey"


# ---------------------------------------------------------------------------
# Baseball
# ---------------------------------------------------------------------------

def test_baseball_parses_score() -> None:
    events = parse_goalserve_ws_events(
        "baseball",
        _state("ev1", sport="baseball", home_score=5, away_score=3),
        observed_at=_OBSERVED,
    )
    ev = events[0]
    assert ev.sport == "baseball"
    assert ev.home.score == 5
    assert ev.away.score == 3
    assert ev.baseball_state is not None


# ---------------------------------------------------------------------------
# Tennis
# ---------------------------------------------------------------------------

def test_tennis_sets_from_stats_g() -> None:
    state = {
        "ev1": {
            **_ws_event("ev1", sport="tennis"),
            "stats": {"g": [2, 1]},
        }
    }
    events = parse_goalserve_ws_events("tennis", state, observed_at=_OBSERVED)
    ev = events[0]
    assert ev.sport == "tennis"
    assert ev.tennis_state is not None
    assert ev.tennis_state.home_sets_won == 2
    assert ev.tennis_state.away_sets_won == 1
    assert ev.home.score == 2
    assert ev.away.score == 1


# ---------------------------------------------------------------------------
# Esports
# ---------------------------------------------------------------------------

def test_esports_maps_from_stats_g() -> None:
    state = {
        "ev1": {
            **_ws_event("ev1", sport="esports"),
            "stats": {"g": [1, 2]},
        }
    }
    events = parse_goalserve_ws_events("esports", state, observed_at=_OBSERVED)
    ev = events[0]
    assert ev.sport == "esports"
    assert ev.esports_state.home_maps_won == 1
    assert ev.esports_state.away_maps_won == 2


# ---------------------------------------------------------------------------
# Odds parsing
# ---------------------------------------------------------------------------

def _ws_event_with_odds(event_id: str) -> dict:
    return {
        event_id: {
            **_ws_event(event_id, sport="basketball"),
            "odds": [
                {
                    "id": 180032,
                    "nm": "Game Lines Money Line",
                    "sp": 0,
                    "o": [
                        {"nm": "Home", "v": "1.09", "hc": "", "sp": 0},
                        {"nm": "Away", "v": "8.0", "hc": "", "sp": 1},
                    ],
                }
            ],
        }
    }


def test_odds_parsed_into_source_payload() -> None:
    events = parse_goalserve_ws_events("basketball", _ws_event_with_odds("ev1"), observed_at=_OBSERVED)
    payload = events[0].source_payload
    assert payload is not None
    assert "goalserve_odds" in payload


def test_odds_implied_prob_calculated() -> None:
    events = parse_goalserve_ws_events("basketball", _ws_event_with_odds("ev1"), observed_at=_OBSERVED)
    markets = events[0].source_payload["goalserve_odds"]["markets"]
    assert len(markets) == 1
    home_outcome = next(o for o in markets[0]["outcomes"] if o["name"] == "Home")
    assert abs(float(home_outcome["implied_prob"]) - (1 / 1.09)) < 0.01


def test_suspended_outcome_flagged() -> None:
    events = parse_goalserve_ws_events("basketball", _ws_event_with_odds("ev1"), observed_at=_OBSERVED)
    markets = events[0].source_payload["goalserve_odds"]["markets"]
    away_outcome = next(o for o in markets[0]["outcomes"] if o["name"] == "Away")
    assert away_outcome["suspended"] is True


def test_suspended_market_flagged() -> None:
    state = {
        "ev1": {
            **_ws_event("ev1", sport="basketball"),
            "odds": [
                {
                    "id": 999,
                    "nm": "Money Line",
                    "sp": 1,
                    "o": [{"nm": "Home", "v": "2.0", "hc": "", "sp": 0}],
                }
            ],
        }
    }
    events = parse_goalserve_ws_events("basketball", state, observed_at=_OBSERVED)
    assert events[0].source_payload["goalserve_odds"]["markets"][0]["suspended"] is True


def test_no_odds_produces_empty_markets() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1"), observed_at=_OBSERVED)
    assert events[0].source_payload["goalserve_odds"]["markets"] == []


# ---------------------------------------------------------------------------
# source_payload / external_ids
# ---------------------------------------------------------------------------

def test_source_payload_carries_event_id() -> None:
    events = parse_goalserve_ws_events("basketball", _ws_event_with_odds("ev42"), observed_at=_OBSERVED)
    assert events[0].source_payload["goalserve_odds"]["event_id"] == "ev42"


def test_external_ids_include_goalserve_key() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev99"), observed_at=_OBSERVED)
    assert events[0].external_ids["goalserve"] == "ev99"


def test_multiple_events_all_parsed() -> None:
    state = {
        "ev1": _ws_event("ev1", home="A", away="B"),
        "ev2": _ws_event("ev2", home="C", away="D"),
        "ev3": _ws_event("ev3", home="E", away="F"),
    }
    events = parse_goalserve_ws_events("soccer", state, observed_at=_OBSERVED)
    assert len(events) == 3


# ---------------------------------------------------------------------------
# seconds_remaining estimation from elapsed time (et)
# ---------------------------------------------------------------------------

def test_soccer_live_regulation_has_seconds_remaining() -> None:
    # et=1800 (30 min played) → remaining = 5400 - 1800 = 3600 sec (pure game clock, no buffer)
    events = parse_goalserve_ws_events("soccer", _state("ev1", sport="soccer", stp=1, et=1800), observed_at=_OBSERVED)
    assert events[0].seconds_remaining == 3600


def test_soccer_live_near_end_has_small_seconds_remaining() -> None:
    # et=5220 (87 min played) → remaining = 5400 - 5220 = 180 sec (enables moneyline entry gate)
    events = parse_goalserve_ws_events("soccer", _state("ev1", sport="soccer", stp=1, et=5220), observed_at=_OBSERVED)
    assert events[0].seconds_remaining == 180


def test_soccer_extra_time_has_positive_seconds_remaining() -> None:
    # et=5700 (95 min, in extra time) → extra_elapsed = 300, remaining = 1800 - 300 = 1500
    events = parse_goalserve_ws_events("soccer", _state("ev1", sport="soccer", stp=1, et=5700), observed_at=_OBSERVED)
    assert events[0].seconds_remaining == 1500


def test_soccer_not_live_has_no_seconds_remaining() -> None:
    events = parse_goalserve_ws_events("soccer", _state("ev1", sport="soccer", stp=3, et=5400), observed_at=_OBSERVED)
    assert events[0].seconds_remaining is None


def test_basketball_live_midgame_has_seconds_remaining() -> None:
    # et=1440 (24 min played, Q2 start) → remaining = 2880 - 1440 = 1440 sec
    events = parse_goalserve_ws_events("basketball", _state("ev1", sport="basketball", stp=1, et=1440), observed_at=_OBSERVED)
    assert events[0].seconds_remaining == 1440


def test_basketball_live_overtime_has_buffer() -> None:
    # et=3000 (> 2880 regulation) → remaining = 300 sec (OT buffer)
    events = parse_goalserve_ws_events("basketball", _state("ev1", sport="basketball", stp=1, et=3000), observed_at=_OBSERVED)
    assert events[0].seconds_remaining == 300


def test_basketball_not_live_has_no_seconds_remaining() -> None:
    events = parse_goalserve_ws_events("basketball", _state("ev1", sport="basketball", stp=3, et=2880), observed_at=_OBSERVED)
    assert events[0].seconds_remaining is None


def test_hockey_live_has_seconds_remaining() -> None:
    # et=1800 (30 min played) → remaining = 3600 - 1800 = 1800 sec
    events = parse_goalserve_ws_events("hockey", _state("ev1", sport="hockey", stp=1, et=1800), observed_at=_OBSERVED)
    assert events[0].seconds_remaining == 1800


def test_hockey_overtime_has_buffer() -> None:
    # et=3700 (> 3600 regulation) → 300 sec OT buffer
    events = parse_goalserve_ws_events("hockey", _state("ev1", sport="hockey", stp=1, et=3700), observed_at=_OBSERVED)
    assert events[0].seconds_remaining == 300
