from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    RaceState,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
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
    assert race.race_state.leader_driver == "Max Verstappen"
    assert race.race_state.leader_team == "Red Bull Racing"
    assert race.race_state.laps_completed == 48
    assert race.race_state.total_laps == 78
    assert race.race_state.status_flag == "green"
    assert len(race.drivers) == 3
    norris = next(d for d in race.drivers if d.name == "Lando Norris")
    assert norris.position == 2
    assert race.source_payload["venue"] == "Circuit de Monaco"


def test_parser_falls_back_to_lowest_position_when_no_competitor_marked_leader_1() -> None:
    """当没有任何 competitor.status.position == 1（比如赛前数据），按最小 position
    选 leader 兜底。"""

    payload = {
        "events": [
            {
                "id": "r1",
                "name": "Bahrain GP",
                "status": {"type": {"state": "in"}, "period": 1},
                "competitions": [
                    {
                        "lapsCompleted": 5,
                        "totalLaps": 57,
                        "competitors": [
                            {
                                "athlete": {"displayName": "Lewis Hamilton"},
                                "team": {"displayName": "Ferrari"},
                                "status": {"position": 3},
                            },
                            {
                                "athlete": {"displayName": "George Russell"},
                                "team": {"displayName": "Mercedes"},
                                "status": {"position": 2},
                            },
                        ],
                    }
                ],
            }
        ]
    }
    races = parse_espn_race_payload(payload, league="f1")
    assert races[0].race_state.leader_driver == "George Russell"
    assert races[0].race_state.leader_team == "Mercedes"


def test_parser_falls_back_to_competitor_displayname_when_no_athlete() -> None:
    payload = {
        "events": [
            {
                "id": "r1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {
                        "competitors": [
                            # 没有 athlete 字段，但 competitor 自带 displayName
                            {"displayName": "Car #24", "status": {"position": 1}},
                        ],
                    }
                ],
            }
        ]
    }
    races = parse_espn_race_payload(payload, league="nascar")
    assert len(races) == 1
    driver = races[0].drivers[0]
    assert driver.name == "Car #24"
    assert races[0].race_state.leader_driver == "Car #24"


def test_parser_drops_driver_with_no_name() -> None:
    """athlete 和 competitor 都没有名字 → 该 competitor 被跳过。"""

    payload = {
        "events": [
            {
                "id": "r1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {
                        "competitors": [
                            {"athlete": {}, "status": {"position": 1}},  # 无名
                            {"athlete": {"displayName": "Max V."}, "status": {"position": 2}},
                        ],
                    }
                ],
            }
        ]
    }
    races = parse_espn_race_payload(payload, league="f1")
    drivers = races[0].drivers
    assert len(drivers) == 1
    assert drivers[0].name == "Max V."
    # leader 兜底找最小 position 的有效 driver
    assert races[0].race_state.leader_driver == "Max V."


def test_parser_handles_competitions_not_sequence() -> None:
    payload = {
        "events": [
            {
                "id": "r1",
                "competitions": {"oops": "mapping"},
            }
        ]
    }
    assert parse_espn_race_payload(payload, league="f1") == ()


def test_parser_uses_event_status_when_competition_status_missing() -> None:
    payload = {
        "events": [
            {
                "id": "r1",
                "status": {"type": {"state": "in"}, "flagState": "yellow"},
                "competitions": [
                    {
                        "competitors": [
                            {"athlete": {"displayName": "X"}, "status": {"position": 1}},
                        ],
                    }
                ],
            }
        ]
    }
    races = parse_espn_race_payload(payload, league="f1")
    assert races[0].status == SportsLiveGameStatus.LIVE
    assert races[0].race_state.status_flag == "yellow"


def test_parser_falls_back_to_competition_status_when_event_status_missing() -> None:
    payload = {
        "events": [
            {
                "id": "r1",
                "competitions": [
                    {
                        "status": {"type": {"state": "post"}},
                        "competitors": [
                            {"athlete": {"displayName": "X"}, "status": {"position": 1}},
                        ],
                    }
                ],
            }
        ]
    }
    races = parse_espn_race_payload(payload, league="nascar")
    assert races[0].status == SportsLiveGameStatus.ENDED


def test_parser_uses_league_uppercase_when_no_event_name() -> None:
    payload = {
        "events": [
            {
                "id": "r1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {"competitors": [{"athlete": {"displayName": "X"}, "status": {"position": 1}}]},
                ],
            }
        ]
    }
    races = parse_espn_race_payload(payload, league="indycar")
    assert races[0].event_name == "INDYCAR"


def test_parser_handles_venue_not_mapping() -> None:
    payload = {
        "events": [
            {
                "id": "r1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {
                        "venue": "Silverstone",  # 不是 mapping
                        "competitors": [{"athlete": {"displayName": "X"}, "status": {"position": 1}}],
                    }
                ],
            }
        ]
    }
    races = parse_espn_race_payload(payload, league="f1")
    assert races[0].source_payload["venue"] is None


def test_parser_ignores_malformed_competitors() -> None:
    payload = {
        "events": [
            {
                "id": "r1",
                "status": {"type": {"state": "in"}},
                "competitions": [
                    {
                        "competitors": [
                            "not-a-mapping",
                            {"athlete": {"displayName": "Valid"}, "status": {"position": 1}},
                            42,
                        ],
                    }
                ],
            }
        ]
    }
    races = parse_espn_race_payload(payload, league="f1")
    drivers = races[0].drivers
    assert len(drivers) == 1
    assert drivers[0].name == "Valid"


def test_parser_handles_event_not_mapping_in_events_list() -> None:
    """payload.events 内混入非 mapping 元素时跳过。"""

    payload = {"events": [None, "garbage", 42, {"id": "valid", "status": {"type": {"state": "in"}}, "competitions": [{"competitors": [{"athlete": {"displayName": "V"}, "status": {"position": 1}}]}]}]}
    races = parse_espn_race_payload(payload, league="f1")
    assert len(races) == 1
    assert races[0].source_event_id == "valid"


def test_parser_returns_empty_for_no_events() -> None:
    assert parse_espn_race_payload({}, league="f1") == ()
    assert parse_espn_race_payload({"events": []}, league="nascar") == ()


def test_aggregate_client_merges_race_events_alongside_games() -> None:
    observed = datetime(2026, 5, 11, 2, 0, tzinfo=timezone.utc)
    race = LiveEvent(
        source="espn",
        source_event_id="race-1",
        kind=LiveEventKind.RACE,
        sport="motorsport",
        participants=(Participant(role="driver", name="Max Verstappen", position=1),),
        league="F1",
        event_name="Imola",
        status=SportsLiveGameStatus.LIVE,
        observed_at=observed,
        race_state=RaceState(leader_driver="Max Verstappen"),
    )
    nba_game = LiveEvent(
        
        participants=(Participant(role="home", name="Celtics", score=100), Participant(role="away", name="Knicks", score=98),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="basketball",source="espn",
        source_event_id="nba-1",
        league="NBA",
        status=SportsLiveGameStatus.LIVE,
        period="Q4",
        observed_at=observed,
    )

    async def provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(
            source="espn",
            observed_at=observed,
            events=(nba_game, race),
        )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(("espn", provider),),
            now_provider=lambda: observed,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    # 新模型 LiveEvent 把 team_match 与 race 放在同一 events 列表；两类不互相 dedup。
    assert len(snapshot.events) == 2
    by_kind = {e.kind: e for e in snapshot.events}
    assert by_kind[LiveEventKind.TEAM_MATCH].source_event_id == "nba-1"
    assert by_kind[LiveEventKind.RACE].event_name == "Imola"


def test_aggregate_client_dedupes_race_events_keeping_newest() -> None:
    base = datetime(2026, 5, 11, tzinfo=timezone.utc)
    older = LiveEvent(
        source="espn", source_event_id="r", kind=LiveEventKind.RACE, sport="motorsport",
        participants=(Participant(role="driver", name="X", position=1),),
        league="F1",
        event_name="Imola", status=SportsLiveGameStatus.LIVE,
        observed_at=base, race_state=RaceState(laps_completed=10),
        external_ids={"espn": "r"},
    )
    newer = LiveEvent(
        source="espn", source_event_id="r", kind=LiveEventKind.RACE, sport="motorsport",
        participants=(Participant(role="driver", name="X", position=1),),
        league="F1",
        event_name="Imola", status=SportsLiveGameStatus.LIVE,
        observed_at=base + timedelta(minutes=5), race_state=RaceState(laps_completed=15),
        external_ids={"espn": "r"},
    )

    async def provider_a() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(source="espn", observed_at=base, events=(older,))

    async def provider_b() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(
            source="thesportsdb",
            observed_at=base + timedelta(minutes=5),
            events=(newer,),
        )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(("espn", provider_a), ("thesportsdb", provider_b)),
            now_provider=lambda: base + timedelta(minutes=5),
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    assert len(snapshot.events) == 1
    assert snapshot.events[0].race_state.laps_completed == 15
