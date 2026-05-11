from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.infra.sports.espn_client import (
    parse_espn_scoreboard_payload,
)


def test_parser_extracts_cricket_state_for_t20_match() -> None:
    payload = {
        "events": [
            {
                "id": "401900001",
                "date": "2026-04-05T14:00:00Z",
                "name": "India vs Australia",
                "shortName": "IND vs AUS",
                "status": {"type": {"state": "in", "name": "STATUS_IN_PROGRESS", "detail": "12.3 overs"}, "period": 1},
                "competitions": [
                    {
                        "id": "401900001-1",
                        "date": "2026-04-05T14:00:00Z",
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "98",
                                "team": {"displayName": "India", "abbreviation": "IND"},
                                "statistics": [
                                    {"name": "wickets", "value": 2, "displayValue": "2"},
                                    {"name": "oversBowled", "value": 12.3, "displayValue": "12.3"},
                                ],
                            },
                            {
                                "homeAway": "away",
                                "score": "0",
                                "team": {"displayName": "Australia", "abbreviation": "AUS"},
                            },
                        ],
                        "situation": {
                            "batting": {"displayName": "India"},
                            "currentInnings": 1,
                            "targetRuns": 175,
                            "requiredRuns": 77,
                            "requiredBalls": 45,
                        },
                    }
                ],
            }
        ]
    }

    games = parse_espn_scoreboard_payload(
        payload,
        league="intl-t20i",
        observed_at=datetime(2026, 4, 5, 14, 30, tzinfo=timezone.utc),
    )

    assert len(games) == 1
    game = games[0]
    assert game.league == "INTL-T20I"
    assert game.home.name == "India"
    assert game.away.name == "Australia"
    state = game.cricket_state
    assert state is not None
    assert state.batting_side == "home"
    assert state.runs == 98
    assert state.wickets == 2
    assert state.overs_completed == 12
    assert state.balls_in_over == 3
    assert state.target == 175
    assert state.required_runs == 77
    assert state.required_balls == 45
    assert state.current_innings == 1


def test_parser_returns_no_cricket_state_for_non_cricket_league() -> None:
    payload = {
        "events": [
            {
                "id": "401900002",
                "status": {"type": {"state": "in"}, "period": 1},
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "score": "0", "team": {"displayName": "Lakers"}},
                            {"homeAway": "away", "score": "0", "team": {"displayName": "Celtics"}},
                        ],
                    }
                ],
            }
        ]
    }
    games = parse_espn_scoreboard_payload(payload, league="nba")
    assert len(games) == 1
    assert games[0].cricket_state is None


def test_parser_omits_state_when_no_cricket_signals() -> None:
    payload = {
        "events": [
            {
                "id": "401900003",
                "status": {"type": {"state": "pre"}, "period": 0},
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "score": "0", "team": {"displayName": "India"}},
                            {"homeAway": "away", "score": "0", "team": {"displayName": "Sri Lanka"}},
                        ],
                    }
                ],
            }
        ]
    }
    games = parse_espn_scoreboard_payload(payload, league="intl-odi")
    assert len(games) == 1
    # No batting/score/wickets/overs/target → cricket_state stays None
    assert games[0].cricket_state is None
