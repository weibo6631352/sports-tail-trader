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


def test_parser_handles_string_stat_values_and_target() -> None:
    """ESPN 实际返回 statistics value 可能是字符串；target 也常以字符串携带。"""

    payload = {
        "events": [
            {
                "id": "1",
                "status": {"type": {"state": "in"}, "period": 1},
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "210",
                                "team": {"displayName": "India"},
                                "statistics": [
                                    {"name": "wickets", "value": "5", "displayValue": "5"},
                                    {"name": "overs", "value": "30", "displayValue": "30"},
                                ],
                            },
                            {"homeAway": "away", "score": "0", "team": {"displayName": "England"}},
                        ],
                        "situation": {
                            "batting": {"displayName": "India"},
                            "targetRuns": "275",
                            "requiredRuns": "65",
                            "requiredBalls": "120",
                        },
                    }
                ],
            }
        ]
    }
    games = parse_espn_scoreboard_payload(payload, league="intl-odi")
    state = games[0].cricket_state
    assert state is not None
    assert state.wickets == 5
    assert state.overs_completed == 30
    assert state.balls_in_over == 0  # "30" 整数形式 → balls_in_over=0
    assert state.target == 275
    assert state.required_runs == 65
    assert state.required_balls == 120


def test_parser_handles_invalid_overs_format_safely() -> None:
    payload = {
        "events": [
            {
                "id": "1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "100",
                                "team": {"displayName": "India"},
                                "statistics": [
                                    {"name": "overs", "value": "garbage", "displayValue": "garbage"},
                                    {"name": "wickets", "value": 3},
                                ],
                            },
                            {"homeAway": "away", "score": "0", "team": {"displayName": "Sri Lanka"}},
                        ],
                        "situation": {"batting": {"displayName": "India"}},
                    }
                ],
            }
        ]
    }
    games = parse_espn_scoreboard_payload(payload, league="intl-test")
    state = games[0].cricket_state
    assert state is not None
    assert state.wickets == 3
    assert state.overs_completed is None
    assert state.balls_in_over is None


def test_parser_rejects_invalid_balls_in_over() -> None:
    """每个 over 仅 6 个合法球，>5 视为非法 → balls_in_over=None。"""

    payload = {
        "events": [
            {
                "id": "1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "100",
                                "team": {"displayName": "India"},
                                "statistics": [
                                    {"name": "overs", "value": "12.9", "displayValue": "12.9"},
                                ],
                            },
                            {"homeAway": "away", "score": "0", "team": {"displayName": "Sri Lanka"}},
                        ],
                        "situation": {"batting": {"displayName": "India"}},
                    }
                ],
            }
        ]
    }
    games = parse_espn_scoreboard_payload(payload, league="intl-t20i")
    state = games[0].cricket_state
    assert state is not None
    assert state.overs_completed == 12
    assert state.balls_in_over is None  # 9 > 5 → 拒绝


def test_parser_returns_none_state_when_only_innings_known() -> None:
    """没有 situation.batting / runs / wickets / overs / target 时不能伪造 cricket
    state；inning 数字孤立不构成可决策态，返回 None。
    """

    payload = {
        "events": [
            {
                "id": "1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "score": "100", "team": {"displayName": "India"}},
                            {"homeAway": "away", "score": "85", "team": {"displayName": "Pakistan"}},
                        ],
                        "situation": {"currentInnings": 2},
                    }
                ],
            }
        ]
    }
    games = parse_espn_scoreboard_payload(payload, league="intl-t20i")
    assert games[0].cricket_state is None


def test_parser_handles_competitors_not_sequence() -> None:
    """少见错误 payload：competitors 字段是 mapping 而非 sequence。"""

    payload = {
        "events": [
            {
                "id": "1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {
                        "competitors": {"unexpected": "shape"},
                    }
                ],
            }
        ]
    }
    games = parse_espn_scoreboard_payload(payload, league="intl-test")
    # 没有 home/away → _parse_event 返回 None
    assert games == ()


def test_parser_handles_statistics_with_missing_name_key() -> None:
    payload = {
        "events": [
            {
                "id": "1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "50",
                                "team": {"displayName": "India"},
                                "statistics": [
                                    {"value": 3},  # 无 name
                                    {"name": None, "value": 99},
                                    {"name": "wickets", "value": 1},
                                ],
                            },
                            {"homeAway": "away", "score": "0", "team": {"displayName": "Nepal"}},
                        ],
                        "situation": {"batting": {"displayName": "India"}},
                    }
                ],
            }
        ]
    }
    games = parse_espn_scoreboard_payload(payload, league="intl-test")
    state = games[0].cricket_state
    assert state is not None
    # 跳过 missing-name 项，wickets=1 正确提取
    assert state.wickets == 1


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
