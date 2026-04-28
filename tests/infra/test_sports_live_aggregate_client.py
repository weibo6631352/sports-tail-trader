from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
    SportsLiveTeam,
)
from polymarket_trader.infra.sports import SportsLiveAggregateClient


def test_aggregate_client_prefers_live_source_over_scheduled_duplicate() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", lambda: _snapshot("espn", _game("espn", SportsLiveGameStatus.SCHEDULED, observed))),
                ("nba", lambda: _snapshot("nba", _game("nba", SportsLiveGameStatus.LIVE, observed + timedelta(seconds=1)))),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_games()

    snapshot = asyncio.run(run())

    assert snapshot.source == "sports_live_aggregate"
    assert len(snapshot.games) == 1
    assert snapshot.games[0].source == "nba"
    assert snapshot.games[0].status == SportsLiveGameStatus.LIVE
    assert [status.source for status in snapshot.source_statuses] == ["espn", "nba"]
    assert [status.games_seen for status in snapshot.source_statuses] == [1, 1]
    assert all(status.success for status in snapshot.source_statuses)


def test_aggregate_client_keeps_healthy_sources_when_one_source_fails() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)

    async def failing_provider() -> SportsLiveSnapshot:
        raise RuntimeError("source unavailable")

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", failing_provider),
                ("nhl", lambda: _snapshot("nhl", _game("nhl", SportsLiveGameStatus.LIVE, observed))),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_games()

    snapshot = asyncio.run(run())

    assert len(snapshot.games) == 1
    assert snapshot.games[0].source == "nhl"
    assert [(status.source, status.success, status.games_seen) for status in snapshot.source_statuses] == [
        ("espn", False, 0),
        ("nhl", True, 1),
    ]
    assert "source unavailable" in (snapshot.source_statuses[0].last_error or "")


def test_aggregate_client_classifies_empty_rate_limited_and_failed_sources() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)

    async def rate_limited_provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(
            source="thesportsdb",
            observed_at=observed,
            games=(),
            source_statuses=(
                SportsLiveSourceStatus(
                    source="thesportsdb",
                    success=False,
                    health=SportsLiveSourceHealth.RATE_LIMITED,
                    games_seen=0,
                    observed_at=observed,
                    last_error="HTTP 429",
                ),
            ),
        )

    async def empty_provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(source="sofascore", observed_at=observed, games=())

    async def failing_provider() -> SportsLiveSnapshot:
        raise RuntimeError("timeout")

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("thesportsdb", rate_limited_provider),
                ("sofascore", empty_provider),
                ("espn", failing_provider),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_games()

    snapshot = asyncio.run(run())

    assert [
        (status.source, status.success, status.health, status.games_seen)
        for status in snapshot.source_statuses
    ] == [
        ("thesportsdb", False, SportsLiveSourceHealth.RATE_LIMITED, 0),
        ("sofascore", True, SportsLiveSourceHealth.SUCCESS_EMPTY, 0),
        ("espn", False, SportsLiveSourceHealth.FAILED, 0),
    ]


def test_aggregate_client_prefers_official_source_over_generic_duplicate() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("sofascore", lambda: _snapshot("sofascore", _game("sofascore", SportsLiveGameStatus.LIVE, observed + timedelta(seconds=2)))),
                ("nba", lambda: _snapshot("nba", _game("nba", SportsLiveGameStatus.LIVE, observed))),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_games()

    snapshot = asyncio.run(run())

    assert len(snapshot.games) == 1
    assert snapshot.games[0].source == "nba"


def test_aggregate_client_keeps_official_status_when_generic_source_conflicts() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    official_game = _game("nba", SportsLiveGameStatus.PAUSED, observed)
    generic_game = _game("sofascore", SportsLiveGameStatus.LIVE, observed + timedelta(seconds=2))

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("nba", lambda: _snapshot("nba", official_game)),
                ("sofascore", lambda: _snapshot("sofascore", generic_game)),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_games()

    snapshot = asyncio.run(run())

    assert len(snapshot.games) == 1
    assert snapshot.games[0].source == "nba"
    assert snapshot.games[0].status == SportsLiveGameStatus.PAUSED
    assert snapshot.games[0].source_payload["source_conflicts"] == (
        {
            "source": "sofascore",
            "status": "live",
            "raw_status": "live",
        },
    )


def test_aggregate_client_deduplicates_abbreviation_and_display_name_sources() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    espn_game = _game("espn", SportsLiveGameStatus.SCHEDULED, observed)
    nba_game = SportsLiveGame(
        source="nba",
        source_event_id="nba-game-1",
        league="NBA",
        home=SportsLiveTeam(
            name="Magic",
            score=87,
            display_name="Orlando Magic",
            abbreviation="ORL",
            short_name="Magic",
            location="Orlando",
        ),
        away=SportsLiveTeam(
            name="Pistons",
            score=85,
            display_name="Detroit Pistons",
            abbreviation="DET",
            short_name="Pistons",
            location="Detroit",
        ),
        status=SportsLiveGameStatus.LIVE,
        period="Q4",
        seconds_remaining=188,
        observed_at=observed,
        raw_status="Q4 3:08",
    )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", lambda: _snapshot("espn", espn_game)),
                ("nba", lambda: _snapshot("nba", nba_game)),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_games()

    snapshot = asyncio.run(run())

    assert len(snapshot.games) == 1
    assert snapshot.games[0].source == "nba"


def test_aggregate_client_deduplicates_full_name_and_short_name_aliases() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    espn_game = SportsLiveGame(
        source="espn",
        source_event_id="espn-nhl-1",
        league="NHL",
        home=SportsLiveTeam(
            name="Utah Mammoth",
            score=0,
            display_name="Utah Mammoth",
            abbreviation="UTA",
            short_name="Mammoth",
        ),
        away=SportsLiveTeam(
            name="Vegas Golden Knights",
            score=2,
            display_name="Vegas Golden Knights",
            abbreviation="VGK",
            short_name="Golden Knights",
        ),
        status=SportsLiveGameStatus.LIVE,
        period="P1",
        seconds_remaining=2400,
        observed_at=observed,
        raw_status="STATUS_IN_PROGRESS",
        source_payload={"start_time_utc": "2026-04-28T01:30Z"},
    )
    nhl_game = SportsLiveGame(
        source="nhl",
        source_event_id="nhl-1",
        league="NHL",
        home=SportsLiveTeam(
            name="Mammoth",
            score=0,
            display_name="Mammoth",
            abbreviation="UTA",
            short_name="Mammoth",
        ),
        away=SportsLiveTeam(
            name="Golden Knights",
            score=2,
            display_name="Golden Knights",
            abbreviation="VGK",
            short_name="Golden Knights",
        ),
        status=SportsLiveGameStatus.PAUSED,
        period="P1",
        seconds_remaining=2837,
        observed_at=observed,
        raw_status="LIVE",
        source_payload={"start_time_utc": "2026-04-28T01:30Z"},
    )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", lambda: _snapshot("espn", espn_game)),
                ("nhl", lambda: _snapshot("nhl", nhl_game)),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_games()

    snapshot = asyncio.run(run())

    assert len(snapshot.games) == 1
    assert snapshot.games[0].source == "espn"


def test_aggregate_client_keeps_same_teams_on_different_start_dates() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    ended_today = SportsLiveGame(
        source="mlb",
        source_event_id="mlb-1",
        league="MLB",
        home=SportsLiveTeam(
            name="Guardians",
            score=2,
            display_name="Cleveland Guardians",
            abbreviation="CLE",
        ),
        away=SportsLiveTeam(
            name="Rays",
            score=3,
            display_name="Tampa Bay Rays",
            abbreviation="TB",
        ),
        status=SportsLiveGameStatus.ENDED,
        period="B9",
        observed_at=observed,
        raw_status="Final",
        source_payload={"game_date": "2026-04-27T22:10:00Z"},
    )
    scheduled_tomorrow = SportsLiveGame(
        source="sofascore",
        source_event_id="sofascore-1",
        league="MLB",
        home=SportsLiveTeam(
            name="Cleveland Guardians",
            score=0,
            display_name="Cleveland Guardians",
            abbreviation="CLE",
        ),
        away=SportsLiveTeam(
            name="Tampa Bay Rays",
            score=0,
            display_name="Tampa Bay Rays",
            abbreviation="TB",
        ),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=observed,
        raw_status="Not started",
        source_payload={"start_timestamp": 1777414200},
    )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("mlb", lambda: _snapshot("mlb", ended_today)),
                ("sofascore", lambda: _snapshot("sofascore", scheduled_tomorrow)),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_games()

    snapshot = asyncio.run(run())

    assert len(snapshot.games) == 2
    assert [game.source for game in snapshot.games] == ["mlb", "sofascore"]


async def _snapshot(source: str, game: SportsLiveGame) -> SportsLiveSnapshot:
    return SportsLiveSnapshot(source=source, observed_at=game.observed_at or datetime.now(timezone.utc), games=(game,))


def _game(source: str, status: SportsLiveGameStatus, observed_at: datetime) -> SportsLiveGame:
    return SportsLiveGame(
        source=source,
        source_event_id=f"{source}-game-1",
        league="NBA",
        home=SportsLiveTeam(
            name="Magic",
            score=78,
            display_name="Orlando Magic",
            abbreviation="ORL",
            short_name="Magic",
            location="Orlando",
        ),
        away=SportsLiveTeam(
            name="Pistons",
            score=76,
            display_name="Detroit Pistons",
            abbreviation="DET",
            short_name="Pistons",
            location="Detroit",
        ),
        status=status,
        period="Q4" if status == SportsLiveGameStatus.LIVE else "STATUS_SCHEDULED",
        seconds_remaining=524 if status == SportsLiveGameStatus.LIVE else None,
        observed_at=observed_at,
        raw_status=status.value,
    )
