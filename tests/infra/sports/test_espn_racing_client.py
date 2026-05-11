from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveRaceEvent,
    SportsLiveSnapshot,
    SportsLiveTeam,
)
from polymarket_trader.infra.sports import SportsLiveAggregateClient
from polymarket_trader.infra.sports.espn_client import parse_espn_race_payload


def test_parser_extracts_race_with_leader_and_laps() -> None:
    payload = {
        "events": [
            {
                "id": "401-race-1",
                "name": "Monaco Grand Prix",
                "shortName": "MON GP",
                "date": "2026-05-25T13:00:00Z",
                "status": {"type": {"state": "in"}, "period": 12, "flagState": "green"},
                "competitions": [
                    {
                        "id": "401-race-1-1",
                        "date": "2026-05-25T13:00:00Z",
                        "lapsCompleted": 48,
                        "totalLaps": 78,
                        "venue": {"fullName": "Circuit de Monaco"},
                        "competitors": [
                            {
                                "athlete": {"displayName": "Max Verstappen", "shortName": "M. Verstappen"},
                                "team": {"displayName": "Red Bull Racing"},
                                "status": {"position": 1, "laps": 48, "displayName": "leader"},
                            },
                            {
                                "athlete": {"displayName": "Lando Norris"},
                                "team": {"displayName": "McLaren"},
                                "status": {"position": 2, "laps": 48},
                                "behindBy": "+2.345",
                            },
                            {
                                "athlete": {"displayName": "Charles Leclerc"},
                                "team": {"displayName": "Ferrari"},
                                "status": {"position": 3, "laps": 47},
                            },
                        ],
                    }
                ],
            }
        ]
    }

    races = parse_espn_race_payload(
        payload,
        league="f1",
        observed_at=datetime(2026, 5, 25, 14, tzinfo=timezone.utc),
    )

    assert len(races) == 1
    race = races[0]
    assert race.league == "F1"
    assert race.event_name == "Monaco Grand Prix"
    assert race.status == SportsLiveGameStatus.LIVE
    assert race.leader_driver == "Max Verstappen"
    assert race.leader_team == "Red Bull Racing"
    assert race.laps_completed == 48
    assert race.total_laps == 78
    assert race.status_flag == "green"
    assert len(race.drivers) == 3
    norris = next(d for d in race.drivers if d.driver == "Lando Norris")
    assert norris.position == 2
    assert norris.gap_to_leader == "+2.345"
    assert race.source_payload["venue"] == "Circuit de Monaco"


def test_parser_returns_empty_for_no_events() -> None:
    assert parse_espn_race_payload({}, league="f1") == ()
    assert parse_espn_race_payload({"events": []}, league="nascar") == ()


def test_aggregate_client_merges_race_events_alongside_games() -> None:
    observed = datetime(2026, 5, 11, 2, 0, tzinfo=timezone.utc)
    race = SportsLiveRaceEvent(
        source="espn",
        source_event_id="race-1",
        league="F1",
        event_name="Imola",
        status=SportsLiveGameStatus.LIVE,
        observed_at=observed,
    )
    nba_game = SportsLiveGame(
        source="espn",
        source_event_id="nba-1",
        league="NBA",
        home=SportsLiveTeam(name="Celtics", score=100),
        away=SportsLiveTeam(name="Knicks", score=98),
        status=SportsLiveGameStatus.LIVE,
        period="Q4",
        observed_at=observed,
    )

    async def provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(
            source="espn",
            observed_at=observed,
            games=(nba_game,),
            race_events=(race,),
        )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(("espn", provider),),
            now_provider=lambda: observed,
        )
        return await client.list_games()

    snapshot = asyncio.run(run())
    assert len(snapshot.games) == 1
    assert snapshot.games[0].source_event_id == "nba-1"
    assert len(snapshot.race_events) == 1
    assert snapshot.race_events[0].event_name == "Imola"


def test_aggregate_client_dedupes_race_events_keeping_newest() -> None:
    base = datetime(2026, 5, 11, tzinfo=timezone.utc)
    older = SportsLiveRaceEvent(
        source="espn", source_event_id="r", league="F1",
        event_name="Imola", status=SportsLiveGameStatus.LIVE,
        observed_at=base, laps_completed=10,
    )
    newer = SportsLiveRaceEvent(
        source="espn", source_event_id="r", league="F1",
        event_name="Imola", status=SportsLiveGameStatus.LIVE,
        observed_at=base + timedelta(minutes=5), laps_completed=15,
    )

    async def provider_a() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(source="espn", observed_at=base, games=(), race_events=(older,))

    async def provider_b() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(
            source="thesportsdb",
            observed_at=base + timedelta(minutes=5),
            games=(),
            race_events=(newer,),
        )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(("espn", provider_a), ("thesportsdb", provider_b)),
            now_provider=lambda: base + timedelta(minutes=5),
        )
        return await client.list_games()

    snapshot = asyncio.run(run())
    assert len(snapshot.race_events) == 1
    assert snapshot.race_events[0].laps_completed == 15
