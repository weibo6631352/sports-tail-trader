from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.infra.sports.espn_standings_client import (
    parse_espn_standings_payload,
)


def test_parses_grouped_standings() -> None:
    payload = {
        "season": {"year": 2026},
        "children": [
            {
                "name": "Eastern Conference",
                "standings": {
                    "entries": [
                        {
                            "team": {"displayName": "Boston Celtics", "abbreviation": "BOS"},
                            "stats": [
                                {"name": "wins", "value": 58},
                                {"name": "losses", "value": 24},
                                {"name": "winpercent", "value": 0.707},
                                {"name": "playoffSeed", "value": 1},
                            ],
                            "clinchIndicator": "x",
                        },
                        {
                            "team": {"displayName": "New York Knicks"},
                            "stats": [
                                {"name": "wins", "value": 50},
                                {"name": "losses", "value": 32},
                            ],
                        },
                    ]
                },
            }
        ],
    }

    standings = parse_espn_standings_payload(
        payload,
        league="nba",
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    assert standings is not None
    assert standings.league == "NBA"
    assert standings.season_id == "2026"
    assert len(standings.rows) == 2
    first = standings.rows[0]
    assert first.team == "Boston Celtics"
    assert first.wins == 58
    assert first.losses == 24
    assert first.conference == "Eastern Conference"
    assert first.seed == 1
    assert first.clinched == "x"


def test_returns_none_when_payload_empty() -> None:
    assert parse_espn_standings_payload({}, league="nba") is None
    assert parse_espn_standings_payload({"standings": {"entries": []}}, league="nba") is None
