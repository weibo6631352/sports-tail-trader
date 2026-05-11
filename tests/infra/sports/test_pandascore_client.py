from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.sports_live import SportsLiveGameStatus
from polymarket_trader.infra.sports.pandascore_client import (
    PandascoreLiveClient,
    parse_pandascore_lives_payload,
)


def test_parser_normalizes_running_csgo_match() -> None:
    payload = [
        {
            "match": {
                "id": 12345,
                "slug": "team-a-vs-team-b-2026-05-11",
                "status": "running",
                "number_of_games": 3,
                "videogame": {"slug": "cs-go", "name": "CS:GO"},
                "tournament": {"name": "ESL Pro League", "slug": "esl-pro-league"},
                "begin_at": "2026-05-11T15:00:00Z",
                "opponents": [
                    {"opponent": {"id": 1, "name": "Team Alpha", "acronym": "ALPHA", "slug": "team-alpha"}},
                    {"opponent": {"id": 2, "name": "Team Beta", "acronym": "BETA", "slug": "team-beta"}},
                ],
                "results": [
                    {"team_id": 1, "score": 1},
                    {"team_id": 2, "score": 0},
                ],
                "games": [
                    {
                        "position": 1,
                        "status": "finished",
                        "winner": {"id": 1, "type": "Team"},
                        "scores": [
                            {"team_id": 1, "score": 16},
                            {"team_id": 2, "score": 9},
                        ],
                    },
                    {
                        "position": 2,
                        "status": "running",
                        "scores": [
                            {"team_id": 1, "score": 7},
                            {"team_id": 2, "score": 11},
                        ],
                    },
                ],
            }
        }
    ]

    games = parse_pandascore_lives_payload(
        payload,
        observed_at=datetime(2026, 5, 11, 16, 30, tzinfo=timezone.utc),
    )

    assert len(games) == 1
    game = games[0]
    assert game.source == "pandascore"
    assert game.league == "CS-GO"
    assert game.home.name == "Team Alpha"
    assert game.away.name == "Team Beta"
    assert game.home.score == 1
    assert game.away.score == 0
    assert game.status == SportsLiveGameStatus.LIVE
    assert game.period == "MAP2"
    assert game.source_payload["sport"] == "esports"
    state = game.esports_state
    assert state is not None
    assert state.best_of == 3
    assert state.current_map_index == 2
    assert state.home_maps_won == 1
    assert state.away_maps_won == 0
    assert state.home_current_map_score == 7
    assert state.away_current_map_score == 11
    assert state.map_winners == ("home",)


def test_parser_handles_data_envelope_and_filters_videogames() -> None:
    payload = {
        "data": [
            {
                "id": 7,
                "status": "running",
                "number_of_games": 5,
                "videogame": {"slug": "dota-2"},
                "begin_at": "2026-05-12T09:00:00Z",
                "opponents": [
                    {"opponent": {"id": 11, "name": "TI Champs", "acronym": "TIC"}},
                    {"opponent": {"id": 12, "name": "Underdogs", "acronym": "UND"}},
                ],
                "results": [
                    {"team_id": 11, "score": 2},
                    {"team_id": 12, "score": 1},
                ],
                "games": [],
            },
            {
                "id": 8,
                "status": "running",
                "videogame": {"slug": "valorant"},
                "opponents": [
                    {"opponent": {"id": 21, "name": "Squad X"}},
                    {"opponent": {"id": 22, "name": "Squad Y"}},
                ],
                "results": [],
                "games": [],
            },
        ]
    }

    games = parse_pandascore_lives_payload(
        payload,
        observed_at=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
        videogame_slug_filter=("dota-2",),
    )

    assert len(games) == 1
    game = games[0]
    assert game.league == "DOTA-2"
    assert game.home.name == "TI Champs"
    assert game.home.score == 2
    assert game.away.score == 1


def test_parser_ignores_malformed_entries() -> None:
    payload = ["not-a-mapping", {}, {"match": {"opponents": []}}]
    games = parse_pandascore_lives_payload(payload, observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc))
    assert games == ()


def test_client_disabled_without_token() -> None:
    client = PandascoreLiveClient(api_token=None)

    assert client.enabled is False
