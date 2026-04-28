from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from polymarket_trader.domain.sports_live import SportsLiveGameStatus, SportsLiveSourceHealth
from polymarket_trader.infra.sports import (
    MlbStatsApiClient,
    NbaLiveScoreboardClient,
    NhlScoreApiClient,
    SofaScoreLiveClient,
    TheSportsDbLiveClient,
    parse_mlb_schedule_payload,
    parse_nba_scoreboard_payload,
    parse_nhl_score_payload,
    parse_sofascore_events_payload,
    parse_thesportsdb_events_payload,
)


def test_nba_scoreboard_parser_normalizes_live_game_clock() -> None:
    games = parse_nba_scoreboard_payload(
        {
            "scoreboard": {
                "games": [
                    {
                        "gameId": "0042500104",
                        "gameStatus": 2,
                        "gameStatusText": "Q4 8:44",
                        "period": 4,
                        "gameClock": "PT08M44.00S",
                        "regulationPeriods": 4,
                        "homeTeam": {
                            "teamName": "Magic",
                            "teamCity": "Orlando",
                            "teamTricode": "ORL",
                            "score": 78,
                        },
                        "awayTeam": {
                            "teamName": "Pistons",
                            "teamCity": "Detroit",
                            "teamTricode": "DET",
                            "score": 76,
                        },
                    }
                ]
            }
        },
        observed_at=datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
    )

    assert len(games) == 1
    game = games[0]
    assert game.source == "nba"
    assert game.source_event_id == "0042500104"
    assert game.league == "NBA"
    assert game.home.name == "Magic"
    assert game.home.display_name == "Orlando Magic"
    assert game.home.abbreviation == "ORL"
    assert game.home.score == 78
    assert game.away.name == "Pistons"
    assert game.away.score == 76
    assert game.status == SportsLiveGameStatus.LIVE
    assert game.period == "Q4"
    assert game.seconds_remaining == 524
    assert game.raw_status == "Q4 8:44"


def test_nhl_score_parser_normalizes_critical_game_clock() -> None:
    games = parse_nhl_score_payload(
        {
            "games": [
                {
                    "id": 2025030145,
                    "gameState": "CRIT",
                    "awayTeam": {"name": {"default": "Flyers"}, "abbrev": "PHI", "score": 2},
                    "homeTeam": {"name": {"default": "Penguins"}, "abbrev": "PIT", "score": 3},
                    "clock": {"secondsRemaining": 82, "running": True, "inIntermission": False},
                    "period": 3,
                    "periodDescriptor": {"number": 3, "maxRegulationPeriods": 3},
                }
            ]
        },
        observed_at=datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
    )

    assert len(games) == 1
    game = games[0]
    assert game.source == "nhl"
    assert game.league == "NHL"
    assert game.home.name == "Penguins"
    assert game.away.name == "Flyers"
    assert game.status == SportsLiveGameStatus.LIVE
    assert game.period == "P3"
    assert game.seconds_remaining == 82
    assert game.raw_status == "CRIT"


def test_mlb_schedule_parser_normalizes_in_progress_game_without_clock() -> None:
    games = parse_mlb_schedule_payload(
        {
            "dates": [
                {
                    "games": [
                        {
                            "gamePk": 824446,
                            "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
                            "teams": {
                                "away": {
                                    "score": 3,
                                    "team": {
                                        "name": "Tampa Bay Rays",
                                        "teamName": "Rays",
                                        "locationName": "Tampa Bay",
                                        "abbreviation": "TB",
                                    },
                                },
                                "home": {
                                    "score": 2,
                                    "team": {
                                        "name": "Cleveland Guardians",
                                        "teamName": "Guardians",
                                        "locationName": "Cleveland",
                                        "abbreviation": "CLE",
                                    },
                                },
                            },
                            "linescore": {
                                "currentInning": 9,
                                "inningHalf": "Bottom",
                                "outs": 2,
                                "offense": {
                                    "team": {"name": "Cleveland Guardians"},
                                    "first": None,
                                    "second": None,
                                    "third": None,
                                },
                                "defense": {"team": {"name": "Tampa Bay Rays"}},
                            },
                        }
                    ]
                }
            ]
        },
        observed_at=datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
    )

    assert len(games) == 1
    game = games[0]
    assert game.source == "mlb"
    assert game.league == "MLB"
    assert game.home.name == "Guardians"
    assert game.away.name == "Rays"
    assert game.status == SportsLiveGameStatus.LIVE
    assert game.period == "B9"
    assert game.seconds_remaining is None
    assert game.raw_status == "In Progress"
    assert game.baseball_state is not None
    assert game.baseball_state.current_inning == 9
    assert game.baseball_state.inning_half == "bottom"
    assert game.baseball_state.outs == 2
    assert game.baseball_state.offense_team == "Cleveland Guardians"
    assert game.baseball_state.defense_team == "Tampa Bay Rays"
    assert game.baseball_state.occupied_bases == ()


def test_sofascore_parser_normalizes_filtered_basketball_live_game() -> None:
    games = parse_sofascore_events_payload(
        {
            "events": [
                {
                    "id": 15935004,
                    "slug": "orlando-magic-detroit-pistons",
                    "status": {"code": 16, "description": "4th quarter", "type": "inprogress"},
                    "tournament": {
                        "name": "NBA",
                        "slug": "nba",
                        "uniqueTournament": {"name": "NBA", "slug": "nba"},
                    },
                    "homeTeam": {
                        "name": "Orlando Magic",
                        "shortName": "Magic",
                        "nameCode": "ORL",
                        "slug": "orlando-magic",
                        "country": {"name": "USA"},
                    },
                    "awayTeam": {
                        "name": "Detroit Pistons",
                        "shortName": "Pistons",
                        "nameCode": "DET",
                        "slug": "detroit-pistons",
                    },
                    "homeScore": {"current": 87, "display": 87},
                    "awayScore": {"current": 85, "display": 85},
                    "time": {
                        "played": 2692,
                        "periodLength": 720,
                        "totalPeriodCount": 4,
                    },
                },
                {
                    "id": 15555555,
                    "status": {"description": "4th quarter", "type": "inprogress"},
                    "tournament": {
                        "name": "Liga ACB",
                        "slug": "liga-acb",
                        "uniqueTournament": {"name": "Liga ACB", "slug": "liga-acb"},
                    },
                    "homeTeam": {"name": "Barcelona", "shortName": "Barcelona", "nameCode": "BAR"},
                    "awayTeam": {"name": "Real Madrid", "shortName": "Real Madrid", "nameCode": "RMA"},
                    "homeScore": {"current": 70},
                    "awayScore": {"current": 72},
                    "time": {"played": 2600, "periodLength": 600, "totalPeriodCount": 4},
                },
            ]
        },
        sport="basketball",
        league_codes=("nba",),
        observed_at=datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
    )

    assert len(games) == 1
    game = games[0]
    assert game.source == "sofascore"
    assert game.source_event_id == "15935004"
    assert game.league == "NBA"
    assert game.home.name == "Orlando Magic"
    assert game.home.short_name == "Magic"
    assert game.home.abbreviation == "ORL"
    assert game.home.score == 87
    assert "USA" not in game.home.match_aliases()
    assert game.away.name == "Detroit Pistons"
    assert game.away.score == 85
    assert game.status == SportsLiveGameStatus.LIVE
    assert game.period == "Q4"
    assert game.seconds_remaining == 188
    assert game.raw_status == "4th quarter"


def test_sofascore_parser_normalizes_finished_baseball_game_without_clock() -> None:
    games = parse_sofascore_events_payload(
        {
            "events": [
                {
                    "id": 15495498,
                    "status": {"code": 100, "description": "Ended", "type": "finished"},
                    "tournament": {
                        "name": "MLB",
                        "slug": "mlb",
                        "uniqueTournament": {"name": "MLB", "slug": "mlb"},
                    },
                    "homeTeam": {"name": "Cleveland Guardians", "shortName": "Guardians", "nameCode": "CLE"},
                    "awayTeam": {"name": "Tampa Bay Rays", "shortName": "Rays", "nameCode": "TB"},
                    "homeScore": {"current": 2, "display": 2},
                    "awayScore": {"current": 3, "display": 3},
                    "time": {"currentPeriodStartTimestamp": 1777336799},
                }
            ]
        },
        sport="baseball",
        league_codes=("mlb",),
        observed_at=datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
    )

    assert len(games) == 1
    game = games[0]
    assert game.source == "sofascore"
    assert game.league == "MLB"
    assert game.home.name == "Cleveland Guardians"
    assert game.away.name == "Tampa Bay Rays"
    assert game.status == SportsLiveGameStatus.ENDED
    assert game.period == "Ended"
    assert game.seconds_remaining is None


def test_thesportsdb_parser_normalizes_filtered_nhl_live_game() -> None:
    games = parse_thesportsdb_events_payload(
        {
            "events": [
                {
                    "idEvent": "2466191",
                    "strSport": "Ice Hockey",
                    "strLeague": "NHL",
                    "strHomeTeam": "Utah Mammoth",
                    "strAwayTeam": "Vegas Golden Knights",
                    "intHomeScore": "0",
                    "intAwayScore": "1",
                    "strStatus": "P1",
                    "dateEvent": "2026-04-28",
                    "strTimestamp": "2026-04-28T01:30:00",
                },
                {
                    "idEvent": "2466185",
                    "strSport": "Ice Hockey",
                    "strLeague": "Swedish Hockey League",
                    "strHomeTeam": "Rögle BK",
                    "strAwayTeam": "Skellefteå AIK",
                    "strStatus": "NS",
                    "dateEvent": "2026-04-28",
                    "strTimestamp": "2026-04-28T17:00:00",
                },
            ]
        },
        sport="Ice Hockey",
        league_codes=("nhl",),
        observed_at=datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
    )

    assert len(games) == 1
    game = games[0]
    assert game.source == "thesportsdb"
    assert game.source_event_id == "2466191"
    assert game.league == "NHL"
    assert game.home.name == "Utah Mammoth"
    assert game.home.score == 0
    assert game.away.name == "Vegas Golden Knights"
    assert game.away.score == 1
    assert game.status == SportsLiveGameStatus.LIVE
    assert game.period == "P1"
    assert game.seconds_remaining is None
    assert game.raw_status == "P1"


def test_thesportsdb_parser_normalizes_baseball_inning_status() -> None:
    games = parse_thesportsdb_events_payload(
        {
            "events": [
                {
                    "idEvent": "2387299",
                    "strSport": "Baseball",
                    "strLeague": "MLB",
                    "strHomeTeam": "Texas Rangers",
                    "strAwayTeam": "New York Yankees",
                    "intHomeScore": "1",
                    "intAwayScore": "4",
                    "strStatus": "IN9",
                    "dateEvent": "2026-04-28",
                    "strTimestamp": "2026-04-28T00:05:00",
                }
            ]
        },
        sport="Baseball",
        league_codes=("mlb",),
        observed_at=datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
    )

    assert len(games) == 1
    game = games[0]
    assert game.source == "thesportsdb"
    assert game.league == "MLB"
    assert game.home.name == "Texas Rangers"
    assert game.away.name == "New York Yankees"
    assert game.status == SportsLiveGameStatus.LIVE
    assert game.period == "I9"
    assert game.seconds_remaining is None


def test_league_clients_use_expected_public_endpoints() -> None:
    requests: list[tuple[str, str | None, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.url.path,
                request.url.params.get("date"),
                request.url.params.get("d"),
                request.url.params.get("s"),
            )
        )
        if "nba" in str(request.url.host):
            return httpx.Response(200, request=request, json={"scoreboard": {"games": []}})
        if "nhle" in str(request.url.host):
            return httpx.Response(200, request=request, json={"games": []})
        if "sofascore" in str(request.url.host):
            return httpx.Response(200, request=request, json={"events": []})
        if "thesportsdb" in str(request.url.host):
            return httpx.Response(200, request=request, json={"events": []})
        return httpx.Response(200, request=request, json={"dates": []})

    async def run() -> None:
        observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
        nba = NbaLiveScoreboardClient(
            client=httpx.AsyncClient(base_url="https://cdn.nba.com", transport=httpx.MockTransport(handler)),
            now_provider=lambda: observed,
        )
        nhl = NhlScoreApiClient(
            client=httpx.AsyncClient(base_url="https://api-web.nhle.com", transport=httpx.MockTransport(handler)),
            now_provider=lambda: observed,
        )
        mlb = MlbStatsApiClient(
            client=httpx.AsyncClient(base_url="https://statsapi.mlb.com", transport=httpx.MockTransport(handler)),
            now_provider=lambda: observed,
        )
        sofascore = SofaScoreLiveClient(
            client=httpx.AsyncClient(base_url="https://www.sofascore.com", transport=httpx.MockTransport(handler)),
            sports=("basketball", "ice-hockey"),
            league_codes=("nba", "nhl"),
            now_provider=lambda: observed,
        )
        thesportsdb = TheSportsDbLiveClient(
            client=httpx.AsyncClient(
                base_url="https://www.thesportsdb.com/api/v1/json/3",
                transport=httpx.MockTransport(handler),
            ),
            sports=("Ice Hockey", "Baseball"),
            league_codes=("nhl", "mlb"),
            now_provider=lambda: observed,
        )

        await nba.list_games()
        await nhl.list_games()
        await mlb.list_games()
        await sofascore.list_games()
        await thesportsdb.list_games()
        await nba.aclose()
        await nhl.aclose()
        await mlb.aclose()
        await sofascore.aclose()
        await thesportsdb.aclose()

    asyncio.run(run())

    assert requests == [
        ("/static/json/liveData/scoreboard/todaysScoreboard_00.json", None, None, None),
        ("/v1/score/2026-04-27", None, None, None),
        ("/api/v1/schedule", "04/27/2026", None, None),
        ("/api/v1/sport/basketball/scheduled-events/2026-04-28", None, None, None),
        ("/api/v1/sport/ice-hockey/scheduled-events/2026-04-28", None, None, None),
        ("/api/v1/json/3/eventsday.php", None, "2026-04-28", "Ice Hockey"),
        ("/api/v1/json/3/eventsday.php", None, "2026-04-28", "Baseball"),
    ]


def test_sofascore_client_reuses_cached_snapshot_inside_min_fetch_interval() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            request=request,
            json={
                "events": [
                    {
                        "id": 15935004,
                        "status": {"description": "4th quarter", "type": "inprogress"},
                        "tournament": {
                            "name": "NBA",
                            "slug": "nba",
                            "uniqueTournament": {"name": "NBA", "slug": "nba"},
                        },
                        "homeTeam": {"name": "Orlando Magic", "shortName": "Magic", "nameCode": "ORL"},
                        "awayTeam": {"name": "Detroit Pistons", "shortName": "Pistons", "nameCode": "DET"},
                        "homeScore": {"current": 87},
                        "awayScore": {"current": 85},
                        "time": {"played": 2692, "periodLength": 720, "totalPeriodCount": 4},
                    }
                ]
            },
        )

    async def run() -> tuple[int, int]:
        observed_values = [
            datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 2, 0, 5, tzinfo=timezone.utc),
        ]

        def now_provider() -> datetime:
            return observed_values.pop(0)

        client = SofaScoreLiveClient(
            client=httpx.AsyncClient(
                base_url="https://www.sofascore.com",
                transport=httpx.MockTransport(handler),
            ),
            sports=("basketball",),
            league_codes=("nba",),
            min_fetch_interval_s=60,
            now_provider=now_provider,
        )
        try:
            first = await client.list_games()
            second = await client.list_games()
            return len(first.games), len(second.games)
        finally:
            await client.aclose()

    first_count, second_count = asyncio.run(run())

    assert first_count == 1
    assert second_count == 1
    assert requests == 1


def test_sofascore_client_returns_rate_limited_status_on_cold_rate_limit() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429, request=request, json={"error": "rate limit"})

    async def run():
        client = SofaScoreLiveClient(
            client=httpx.AsyncClient(
                base_url="https://www.sofascore.com",
                transport=httpx.MockTransport(handler),
            ),
            sports=("basketball",),
            league_codes=("nba",),
            min_fetch_interval_s=60,
            now_provider=lambda: datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
        )
        try:
            return await client.list_games()
        finally:
            await client.aclose()

    snapshot = asyncio.run(run())

    assert len(snapshot.games) == 0
    assert snapshot.source_statuses[0].health == SportsLiveSourceHealth.RATE_LIMITED
    assert snapshot.source_statuses[0].success is False
    assert requests == 1


def test_thesportsdb_client_reuses_cached_snapshot_inside_min_fetch_interval() -> None:
    requests: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.params.get("s"))
        return httpx.Response(
            200,
            request=request,
            json={
                "events": [
                    {
                        "idEvent": "2387299",
                        "strSport": "Baseball",
                        "strLeague": "MLB",
                        "strHomeTeam": "Texas Rangers",
                        "strAwayTeam": "New York Yankees",
                        "intHomeScore": "1",
                        "intAwayScore": "4",
                        "strStatus": "IN9",
                        "dateEvent": "2026-04-28",
                        "strTimestamp": "2026-04-28T00:05:00",
                    }
                ]
            },
        )

    async def run() -> tuple[int, int]:
        observed_values = [
            datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 2, 0, 5, tzinfo=timezone.utc),
        ]

        def now_provider() -> datetime:
            return observed_values.pop(0)

        client = TheSportsDbLiveClient(
            client=httpx.AsyncClient(
                base_url="https://www.thesportsdb.com/api/v1/json/3",
                transport=httpx.MockTransport(handler),
            ),
            sports=("Baseball",),
            league_codes=("mlb",),
            min_fetch_interval_s=60,
            now_provider=now_provider,
        )
        try:
            first = await client.list_games()
            second = await client.list_games()
            return len(first.games), len(second.games)
        finally:
            await client.aclose()

    first_count, second_count = asyncio.run(run())

    assert first_count == 1
    assert second_count == 1
    assert requests == ["Baseball"]


def test_thesportsdb_client_uses_cache_when_rate_limited_after_success() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                200,
                request=request,
                json={
                    "events": [
                        {
                            "idEvent": "2466191",
                            "strSport": "Ice Hockey",
                            "strLeague": "NHL",
                            "strHomeTeam": "Utah Mammoth",
                            "strAwayTeam": "Vegas Golden Knights",
                            "intHomeScore": "0",
                            "intAwayScore": "1",
                            "strStatus": "P1",
                            "dateEvent": "2026-04-28",
                            "strTimestamp": "2026-04-28T01:30:00",
                        }
                    ]
                },
            )
        return httpx.Response(429, request=request, json={"error": "rate limit"})

    async def run() -> tuple[int, int]:
        observed_values = [
            datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 2, 2, tzinfo=timezone.utc),
        ]

        def now_provider() -> datetime:
            return observed_values.pop(0)

        client = TheSportsDbLiveClient(
            client=httpx.AsyncClient(
                base_url="https://www.thesportsdb.com/api/v1/json/3",
                transport=httpx.MockTransport(handler),
            ),
            sports=("Ice Hockey",),
            league_codes=("nhl",),
            min_fetch_interval_s=0,
            max_stale_on_error_s=300,
            now_provider=now_provider,
        )
        try:
            first = await client.list_games()
            second = await client.list_games()
            return len(first.games), len(second.games)
        finally:
            await client.aclose()

    first_count, second_count = asyncio.run(run())

    assert first_count == 1
    assert second_count == 1
    assert requests == 2


def test_thesportsdb_client_returns_empty_snapshot_on_cold_rate_limit() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429, request=request, json={"error": "rate limit"})

    async def run():
        client = TheSportsDbLiveClient(
            client=httpx.AsyncClient(
                base_url="https://www.thesportsdb.com/api/v1/json/3",
                transport=httpx.MockTransport(handler),
            ),
            sports=("Ice Hockey",),
            league_codes=("nhl",),
            min_fetch_interval_s=60,
            now_provider=lambda: datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
        )
        try:
            return await client.list_games()
        finally:
            await client.aclose()

    snapshot = asyncio.run(run())

    assert len(snapshot.games) == 0
    assert snapshot.source_statuses[0].health == SportsLiveSourceHealth.RATE_LIMITED
    assert snapshot.source_statuses[0].success is False
    assert requests == 1
