from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from polymarket_trader.domain.sports_live import SportsLiveGameStatus
from polymarket_trader.infra.sports import EspnScoreboardClient, SportsDataRateLimitError


def test_espn_scoreboard_client_normalizes_live_basketball_game() -> None:
    async def run() -> None:
        client = EspnScoreboardClient(
            base_url="https://example.test",
            leagues=("nba",),
            date_window_days_before=0,
            date_window_days_after=0,
            now_provider=lambda: datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc),
            client=httpx.AsyncClient(
                base_url="https://example.test",
                transport=httpx.MockTransport(_scoreboard_handler),
            ),
        )

        snapshot = await client.list_games()
        await client.aclose()

        assert snapshot.source == "espn"
        assert len(snapshot.games) == 1
        game = snapshot.games[0]
        assert game.league == "NBA"
        assert game.home.name == "Knicks"
        assert game.home.score == 102
        assert game.away.name == "Celtics"
        assert game.away.score == 94
        assert game.status == SportsLiveGameStatus.LIVE
        assert game.period == "Q4"
        assert game.seconds_remaining == 90
        assert game.home.match_aliases()[:3] == ("Knicks", "New York Knicks", "NYK")

    asyncio.run(run())


def test_espn_scoreboard_client_maps_429_to_rate_limit_error() -> None:
    async def run() -> None:
        client = EspnScoreboardClient(
            base_url="https://example.test",
            leagues=("nba",),
            date_window_days_before=0,
            date_window_days_after=0,
            now_provider=lambda: datetime(2026, 4, 27, tzinfo=timezone.utc),
            client=httpx.AsyncClient(
                base_url="https://example.test",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        429,
                        request=request,
                        headers={"retry-after": "3"},
                        json={"message": "slow down"},
                    )
                ),
            ),
        )

        try:
            await client.list_games()
        except SportsDataRateLimitError as exc:
            assert exc.status_code == 429
            assert exc.retry_after_s == 3
            assert exc.operation == "espn_scoreboard:nba"
        else:  # pragma: no cover - 明确要求错误分支
            raise AssertionError("expected SportsDataRateLimitError")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_espn_scoreboard_client_requests_only_current_scoreboard_date_by_default() -> None:
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.url.params.get("dates")))
        return httpx.Response(200, request=request, json={"events": []})

    async def run() -> None:
        client = EspnScoreboardClient(
            base_url="https://example.test",
            leagues=("nba",),
            now_provider=lambda: datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc),
            client=httpx.AsyncClient(
                base_url="https://example.test",
                transport=httpx.MockTransport(handler),
            ),
        )

        await client.list_games()
        await client.aclose()

    asyncio.run(run())

    assert requests == [
        ("/apis/site/v2/sports/basketball/nba/scoreboard", "20260427"),
    ]


def test_espn_scoreboard_client_ignores_unsupported_env_proxy(monkeypatch) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")

    client = EspnScoreboardClient(base_url="https://example.test", leagues=("nba",))

    asyncio.run(client.aclose())


def _scoreboard_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/apis/site/v2/sports/basketball/nba/scoreboard"
    assert request.url.params.get("dates") == "20260427"
    return httpx.Response(
        200,
        request=request,
        json={
            "events": [
                {
                    "id": "401705460",
                    "name": "Boston Celtics at New York Knicks",
                    "shortName": "BOS @ NYK",
                    "status": {
                        "clock": 90.0,
                        "displayClock": "1:30",
                        "period": 4,
                        "type": {
                            "name": "STATUS_IN_PROGRESS",
                            "state": "in",
                            "description": "In Progress",
                            "detail": "1:30 - 4th Quarter",
                        },
                    },
                    "competitions": [
                        {
                            "id": "401705460",
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "score": "102",
                                    "team": {
                                        "name": "Knicks",
                                        "displayName": "New York Knicks",
                                        "shortDisplayName": "Knicks",
                                        "abbreviation": "NYK",
                                        "location": "New York",
                                    },
                                },
                                {
                                    "homeAway": "away",
                                    "score": "94",
                                    "team": {
                                        "name": "Celtics",
                                        "displayName": "Boston Celtics",
                                        "shortDisplayName": "Celtics",
                                        "abbreviation": "BOS",
                                        "location": "Boston",
                                    },
                                },
                            ],
                        }
                    ],
                }
            ]
        },
    )
