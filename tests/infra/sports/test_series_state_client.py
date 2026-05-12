"""ESPN scoreboard series payload → SeriesState 解析。"""

from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.infra.sports.series_state_client import (
    parse_espn_scoreboard_series,
)


_OBS = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def _payload(series_key: str = "evt-1") -> dict:
    return {
        "events": [
            {
                "id": series_key,
                "uid": "uid-1",
                "name": "Celtics @ Knicks",
                "shortName": "BOS @ NYK",
                "date": "2026-05-15T23:30:00Z",
                "competitions": [
                    {
                        "id": "comp-1",
                        "series": {
                            "type": 7,
                            "totalCompetitions": 7,
                            "summary": "Series tied 2-2",
                        },
                        "competitors": [
                            {
                                "team": {"displayName": "Boston Celtics"},
                                "records": [
                                    {"type": "series", "summary": "2-1"},
                                ],
                            },
                            {
                                "team": {"displayName": "New York Knicks"},
                                "records": [
                                    {"type": "series", "summary": "1-2"},
                                ],
                            },
                        ],
                    }
                ],
            }
        ]
    }


def test_parses_series_state_from_scoreboard_payload() -> None:
    state = parse_espn_scoreboard_series(
        _payload(),
        sport_key="nba",
        series_key="evt-1",
        observed_at=_OBS,
    )
    assert state is not None
    assert state.team_a == "Boston Celtics"
    assert state.team_b == "New York Knicks"
    assert state.wins_a == 2
    assert state.wins_b == 1
    assert state.best_of == 7
    assert state.observed_at == _OBS
    assert state.next_game_at is not None


def test_returns_none_when_event_not_found() -> None:
    state = parse_espn_scoreboard_series(
        _payload(series_key="other-event"),
        sport_key="nba",
        series_key="not-matching",
        observed_at=_OBS,
    )
    assert state is None


def test_falls_back_to_default_best_of_for_sport() -> None:
    payload = _payload()
    payload["events"][0]["competitions"][0]["series"] = {"summary": "—"}
    state = parse_espn_scoreboard_series(
        payload,
        sport_key="nba",
        series_key="evt-1",
        observed_at=_OBS,
    )
    assert state is not None
    assert state.best_of == 7


def test_invalid_payload_returns_none() -> None:
    # 缺 events 数组
    assert (
        parse_espn_scoreboard_series(
            {"foo": "bar"},
            sport_key="nba",
            series_key="x",
            observed_at=_OBS,
        )
        is None
    )


def test_competitor_without_records_yields_zero_wins() -> None:
    payload = _payload()
    payload["events"][0]["competitions"][0]["competitors"][0]["records"] = []
    state = parse_espn_scoreboard_series(
        payload,
        sport_key="nba",
        series_key="evt-1",
        observed_at=_OBS,
    )
    assert state is not None
    assert state.wins_a == 0
    assert state.wins_b == 1


def test_series_matches_on_short_name() -> None:
    state = parse_espn_scoreboard_series(
        _payload(),
        sport_key="nba",
        series_key="BOS @ NYK",  # 匹配 event.shortName
        observed_at=_OBS,
    )
    assert state is not None
