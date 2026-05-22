"""Goalserve livescore parser unit tests: per-sport parsing, status mapping, empty-data safety.

All fixtures use the actual Goalserve API structure (calibrated against real responses):
  - Top-level wrapper: {"scores": {...}}
  - Team sports (cricket/handball/rugby/boxing/mma): scores.category[].match[]
  - Golf: scores.tournament[].player[]  (pos field, may be "T1" for ties)
  - Horse racing: scores.tournament[].race[]  runners={"horse": [...]}
  - MotoGP/F1: scores.tournament[].{session_key}.results.driver[]
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polymarket_trader.domain.sports_live import (
    LiveEventKind,
    SportsLiveGameStatus,
)
from polymarket_trader.infra.sports.goalserve_livescore_parsers import parse_goalserve_livescore_sport

_OBSERVED = datetime(2026, 5, 20, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers — mirrors real Goalserve response structure
# ---------------------------------------------------------------------------


def _scores_team_wrap(*matches: dict) -> dict:
    """Wrap matches in the real Goalserve category→match structure."""
    return {"scores": {"category": [{"match": list(matches)}]}}


def _team_match(
    id: str = "m1",
    status: str = "In Progress",
    home_name: str = "Home",
    away_name: str = "Away",
    home_total: str = "10",
    away_total: str = "8",
    home_t1: str = "5",
    away_t1: str = "4",
    **extra: object,
) -> dict:
    return {
        "id": id,
        "status": status,
        "localteam": {"name": home_name, "totalscore": home_total, "t1": home_t1, "t2": "0"},
        "awayteam": {"name": away_name, "totalscore": away_total, "t1": away_t1, "t2": "0"},
        **extra,
    }


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_status, expected",
    [
        ("In Progress", SportsLiveGameStatus.LIVE),
        ("Inprogress", SportsLiveGameStatus.LIVE),
        ("Live", SportsLiveGameStatus.LIVE),
        ("Finished", SportsLiveGameStatus.ENDED),
        ("Final", SportsLiveGameStatus.ENDED),
        ("FT", SportsLiveGameStatus.ENDED),
        ("Not Started", SportsLiveGameStatus.SCHEDULED),
        ("Postponed", SportsLiveGameStatus.UNKNOWN),
        ("", SportsLiveGameStatus.UNKNOWN),
    ],
)
def test_text_status_mapping(raw_status: str, expected: SportsLiveGameStatus) -> None:
    data = _scores_team_wrap(
        {
            "id": "1",
            "status": raw_status,
            "localteam": {"name": "Home", "totalscore": "0"},
            "awayteam": {"name": "Away", "totalscore": "0"},
        }
    )
    events = parse_goalserve_livescore_sport("handball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status == expected


# ---------------------------------------------------------------------------
# Empty data safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sport", [
    "cricket", "esports", "handball", "rugby", "boxing", "mma",
    "golf_pga", "golf_dp", "golf_liv", "golf_lpga",
    "horse_racing_us", "horse_racing_uk", "horse_racing_au", "horse_racing_hk",
    "f1", "motogp",
])
def test_empty_data_returns_empty_list(sport: str) -> None:
    assert parse_goalserve_livescore_sport(sport, {}, observed_at=_OBSERVED) == []


def test_null_scores_returns_empty_list() -> None:
    # F1 between races has {"scores": null}
    assert parse_goalserve_livescore_sport("f1", {"scores": None}, observed_at=_OBSERVED) == []


def test_unknown_sport_returns_empty_list() -> None:
    assert parse_goalserve_livescore_sport("unknown_sport", {"scores": {}}, observed_at=_OBSERVED) == []


def test_non_dict_data_returns_empty_list() -> None:
    assert parse_goalserve_livescore_sport("cricket", [], observed_at=_OBSERVED) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cricket
# ---------------------------------------------------------------------------


def test_cricket_basic_parse() -> None:
    data = {
        "scores": {
            "category": [
                {
                    "match": [
                        {
                            "id": "cr_001",
                            "status": "In Progress",
                            "localteam": {"name": "India", "totalscore": "250"},
                            "awayteam": {"name": "Australia", "totalscore": "180"},
                            "time": "45.3",
                            "competition": "Test Series",
                            "innings": [
                                {"number": "1", "batting_team": "1", "runs": "250", "wickets": "6"}
                            ],
                        }
                    ]
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("cricket", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.sport == "cricket"
    assert ev.kind == LiveEventKind.TEAM_MATCH
    assert ev.status == SportsLiveGameStatus.LIVE
    assert ev.home is not None and ev.home.name == "India"
    assert ev.away is not None and ev.away.name == "Australia"
    assert ev.cricket_state is not None
    assert ev.cricket_state.overs_completed == 45
    assert ev.cricket_state.balls_in_over == 3
    assert ev.cricket_state.runs == 250
    assert ev.cricket_state.wickets == 6
    assert ev.cricket_state.batting_side == "home"
    assert ev.source == "goalserve_livescore"


def test_cricket_missing_innings_is_safe() -> None:
    data = _scores_team_wrap(
        {"id": "c1", "status": "Not Started", "localteam": {"name": "A"}, "awayteam": {"name": "B"}}
    )
    events = parse_goalserve_livescore_sport("cricket", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].cricket_state is not None
    assert events[0].cricket_state.runs is None


# ---------------------------------------------------------------------------
# Handball
# ---------------------------------------------------------------------------


def test_handball_basic_parse() -> None:
    data = _scores_team_wrap(
        {
            "id": "hb_1",
            "status": "In Progress",
            "status_str": "1st half",
            "league": "EHF Champions League",
            "localteam": {"name": "THW Kiel", "totalscore": "15", "t1": "15", "t2": "0"},
            "awayteam": {"name": "Barcelona", "totalscore": "13", "t1": "13", "t2": "0"},
        }
    )
    events = parse_goalserve_livescore_sport("handball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.sport == "handball"
    assert ev.status == SportsLiveGameStatus.LIVE
    assert ev.handball_state is not None
    assert ev.handball_state.period == "first_half"
    # Goalserve time field is kickoff time (HH:MM), not game clock — always None
    assert ev.handball_state.clock_minutes is None
    assert ev.handball_state.home_period1 == 15
    assert ev.handball_state.away_period1 == 13


def test_handball_finished_with_period_scores() -> None:
    data = _scores_team_wrap(
        {
            "id": "hb_2",
            "status": "Finished",
            "status_str": "2nd half",
            "localteam": {"name": "Omsk", "totalscore": "43", "t1": "23", "t2": "20"},
            "awayteam": {"name": "Saratov", "totalscore": "40", "t1": "17", "t2": "23"},
        }
    )
    events = parse_goalserve_livescore_sport("handball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.status == SportsLiveGameStatus.ENDED
    home = next(p for p in ev.participants if p.role == "home")
    away = next(p for p in ev.participants if p.role == "away")
    assert home.score == 43
    assert away.score == 40
    assert ev.handball_state.home_period1 == 23
    assert ev.handball_state.away_period1 == 17


# ---------------------------------------------------------------------------
# Rugby
# ---------------------------------------------------------------------------


def test_rugby_basic_parse() -> None:
    data = _scores_team_wrap(
        {
            "id": "rg_1",
            "status": "In Progress",
            "status_str": "2nd half",
            "league": "Premiership",
            "localteam": {"name": "Saracens", "totalscore": "21", "t1": "14", "t2": "7"},
            "awayteam": {"name": "Exeter", "totalscore": "18", "t1": "10", "t2": "8"},
        }
    )
    events = parse_goalserve_livescore_sport("rugby", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.sport == "rugby"
    assert ev.rugby_state is not None
    assert ev.rugby_state.period == "second_half"
    assert ev.rugby_state.clock_minutes is None
    assert ev.rugby_state.home_period1 == 14
    assert ev.rugby_state.away_period1 == 10


def test_rugby_real_format_status_is_period_with_timer() -> None:
    # 真实 rugby/home feed：status 字段即赛段，timer = 已过分钟数（全场 80 分钟）。
    data = _scores_team_wrap(
        {
            "id": "rg_2",
            "status": "2nd Half",
            "timer": "62",
            "localteam": {"name": "Waratahs", "totalscore": "7"},
            "awayteam": {"name": "Brumbies", "totalscore": "14"},
        }
    )
    events = parse_goalserve_livescore_sport("rugby", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.status == SportsLiveGameStatus.LIVE
    assert ev.rugby_state is not None
    assert ev.rugby_state.period == "second_half"
    # 80 - 62 = 18 分钟剩余。
    assert ev.seconds_remaining == 18 * 60


# ---------------------------------------------------------------------------
# Esports
# ---------------------------------------------------------------------------


def _esports_match(
    id: str = "394947",
    status: str = "Finished",
    round_: str = "BO3",
    home_name: str = "Team Falcons",
    away_name: str = "Legacy",
    home_score: str = "2",
    away_score: str = "0",
) -> dict:
    """真实 esports/home getfeed 单场结构（calibrated 自 live fetch）。"""
    return {
        "@status": status,
        "@id": id,
        "@league_id": "8784",
        "@league": "CS Asia Championships Group A",
        "@round": round_,
        "@type": "CS GO",
        "@timer": "",
        "@date": "22.05.2026",
        "@time": "03:00",
        "localteam": {"@name": home_name, "@id": "25464", "@score": home_score},
        "awayteam": {"@name": away_name, "@id": "6787", "@score": away_score},
        "scoreboard": None,
        "maps": None,
        "streams": None,
    }


def test_esports_single_match_dict() -> None:
    # esports getfeed 的 match 直接挂 scores.match，可能是单个 dict。
    data = {"scores": {"@sport": "esports", "match": _esports_match()}}
    events = parse_goalserve_livescore_sport("esports", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.sport == "esports"
    assert ev.kind == LiveEventKind.TEAM_MATCH
    assert ev.source_event_id == "394947"
    assert ev.status == SportsLiveGameStatus.ENDED
    assert ev.participants[0].name == "Team Falcons"
    assert ev.participants[0].score == 2
    assert ev.participants[1].name == "Legacy"
    assert ev.participants[1].score == 0
    assert ev.esports_state is not None
    assert ev.esports_state.best_of == 3
    assert ev.esports_state.home_maps_won == 2
    assert ev.esports_state.away_maps_won == 0


def test_esports_match_list() -> None:
    # match 也可以是 list——两种形态都要展开。
    data = {
        "scores": {
            "match": [
                _esports_match(id="1", status="Started", home_score="1", away_score="0"),
                _esports_match(id="2", status="Not Started", home_score="0", away_score="0"),
            ]
        }
    }
    events = parse_goalserve_livescore_sport("esports", data, observed_at=_OBSERVED)
    assert len(events) == 2
    assert events[0].status == SportsLiveGameStatus.LIVE
    assert events[1].status == SportsLiveGameStatus.SCHEDULED


@pytest.mark.parametrize(
    "raw_status, expected",
    [
        ("Not Started", SportsLiveGameStatus.SCHEDULED),
        ("Started", SportsLiveGameStatus.LIVE),
        ("Finished", SportsLiveGameStatus.ENDED),
        ("Awarded", SportsLiveGameStatus.ENDED),
        ("Cancelled", SportsLiveGameStatus.CANCELLED),
        ("Postponed", SportsLiveGameStatus.POSTPONED),
        ("Weird", SportsLiveGameStatus.UNKNOWN),
    ],
)
def test_esports_status_mapping(raw_status: str, expected: SportsLiveGameStatus) -> None:
    data = {"scores": {"match": _esports_match(status=raw_status)}}
    events = parse_goalserve_livescore_sport("esports", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status == expected


@pytest.mark.parametrize(
    "round_raw, expected_best_of",
    [
        ("BO1", 1),
        ("BO3", 3),
        ("BO5", 5),
        ("bo3", 3),
        ("", None),
        ("BEST OF 3", None),
    ],
)
def test_esports_best_of_parse(round_raw: str, expected_best_of: int | None) -> None:
    data = {"scores": {"match": _esports_match(round_=round_raw)}}
    events = parse_goalserve_livescore_sport("esports", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].esports_state is not None
    assert events[0].esports_state.best_of == expected_best_of


def test_esports_match_without_id_skipped() -> None:
    data = {"scores": {"match": {"@status": "Started", "@round": "BO3"}}}
    assert parse_goalserve_livescore_sport("esports", data, observed_at=_OBSERVED) == []


# ---------------------------------------------------------------------------
# Boxing
# ---------------------------------------------------------------------------


def test_boxing_basic_parse() -> None:
    data = _scores_team_wrap(
        {
            "id": "bx_1",
            "status": "Finished",
            "league": "WBC",
            "round": "8",
            "total_rounds": "12",
            "localteam": {"name": "Canelo", "totalscore": "0", "winner": "yes"},
            "awayteam": {"name": "GGG", "totalscore": "0"},
        }
    )
    events = parse_goalserve_livescore_sport("boxing", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.sport == "boxing"
    assert ev.status == SportsLiveGameStatus.ENDED
    assert ev.mma_state is not None
    assert ev.mma_state.current_round == 8
    assert ev.mma_state.total_rounds == 12
    assert ev.mma_state.winner_side == "home"


# ---------------------------------------------------------------------------
# MMA
# ---------------------------------------------------------------------------


def test_mma_basic_parse() -> None:
    # MMA uses the same category→match structure, not tournament
    data = {
        "scores": {
            "category": [
                {
                    "match": [
                        {
                            "id": "101794",
                            "status": "Finished",
                            "localteam": {"id": "100501", "name": "Ivan Erslan", "winner": "True"},
                            "awayteam": {"id": "98572", "name": "Tuco Tokkos", "winner": "False"},
                            "win_result": {"won_by": "Decision"},
                        }
                    ]
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("mma", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.sport == "mma"
    assert ev.status == SportsLiveGameStatus.ENDED
    assert ev.mma_state is not None
    assert ev.mma_state.result_method == "Decision"
    assert ev.mma_state.winner_side == "home"


def test_mma_winner_from_team_field() -> None:
    # winner_side resolved from awayteam.winner="True" when localteam.winner="False"
    data = _scores_team_wrap(
        {
            "id": "mma_2",
            "status": "Final",
            "localteam": {"name": "Fighter A", "winner": "False"},
            "awayteam": {"name": "Fighter B", "winner": "True"},
        }
    )
    events = parse_goalserve_livescore_sport("mma", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].mma_state.winner_side == "away"


def test_mma_empty_scores_returns_empty() -> None:
    events = parse_goalserve_livescore_sport("mma", {"scores": {}}, observed_at=_OBSERVED)
    assert events == []


# ---------------------------------------------------------------------------
# Golf
# ---------------------------------------------------------------------------


def test_golf_pga_basic_parse() -> None:
    data = {
        "scores": {
            "tournament": [
                {
                    "id": "1038",
                    "name": "PGA Championship",
                    "status": "In Progress",
                    "player": [
                        {"id": "9490", "name": "Aaron Rai", "pos": "1", "country": "ENG"},
                        {"id": "1234", "name": "Scottie Scheffler", "pos": "T2", "country": "USA"},
                    ],
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("golf_pga", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == LiveEventKind.TOURNAMENT_FIELD
    assert ev.sport == "golf"
    assert ev.event_name == "PGA Championship"
    assert len(ev.participants) == 2
    assert ev.participants[0].role == "player"
    assert ev.participants[0].position == 1
    # "T2" tied position strips the "T" prefix → 2
    assert ev.participants[1].position == 2


def test_golf_dp_uses_golf_sport_key() -> None:
    data = {
        "scores": {
            "tournament": [{"id": "dp_1", "name": "BMW PGA", "status": "Finished", "player": []}]
        }
    }
    events = parse_goalserve_livescore_sport("golf_dp", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].sport == "golf"


def test_golf_all_variants_parse() -> None:
    data = {
        "scores": {"tournament": [{"id": "t1", "name": "Test", "status": "Not Started", "player": []}]}
    }
    for sport in ("golf_pga", "golf_dp", "golf_liv", "golf_lpga"):
        events = parse_goalserve_livescore_sport(sport, data, observed_at=_OBSERVED)
        assert len(events) == 1
        assert events[0].sport == "golf"


# ---------------------------------------------------------------------------
# Horse Racing
# ---------------------------------------------------------------------------


def test_horse_racing_basic_parse() -> None:
    data = {
        "scores": {
            "tournament": [
                {
                    "name": "Assiniboia Downs",
                    "race": [
                        {
                            "id": "869019",
                            "name": "Race 1 Maiden Claiming",
                            "results": None,  # None = SCHEDULED (not yet run)
                            "runners": {
                                "horse": [
                                    {
                                        "id": "483206",
                                        "name": "She's So Croatian",
                                        "number": "1",
                                        "jockey": "J R Patterson",
                                    },
                                    {
                                        "id": "483207",
                                        "name": "Good Magic",
                                        "number": "2",
                                        "jockey": "M. Smith",
                                    },
                                ]
                            },
                        }
                    ],
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("horse_racing_us", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == LiveEventKind.RACE
    assert ev.sport == "horse_racing"
    assert ev.status == SportsLiveGameStatus.SCHEDULED
    assert ev.league == "Assiniboia Downs"
    assert len(ev.participants) == 2
    p = ev.participants[0]
    assert p.role == "driver"
    assert p.name == "She's So Croatian"
    assert p.position == 1  # starting number, not result position
    assert p.team == "J R Patterson"  # jockey


def test_horse_racing_finished_status() -> None:
    data = {
        "scores": {
            "tournament": [
                {
                    "name": "Churchill Downs",
                    "race": [
                        {
                            "id": "race_done",
                            "name": "Kentucky Derby",
                            "results": {"winner": "Horse A"},  # non-None → ENDED
                            "runners": {"horse": []},
                        }
                    ],
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("horse_racing_us", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status == SportsLiveGameStatus.ENDED


def test_horse_racing_uk_au_hk_all_parse() -> None:
    data = {
        "scores": {
            "tournament": [
                {"name": "Venue", "race": [{"id": "r1", "name": "Race 1", "results": None, "runners": {"horse": []}}]}
            ]
        }
    }
    for sport in ("horse_racing_uk", "horse_racing_au", "horse_racing_hk"):
        events = parse_goalserve_livescore_sport(sport, data, observed_at=_OBSERVED)
        assert len(events) == 1
        assert events[0].sport == "horse_racing"


# ---------------------------------------------------------------------------
# F1 / MotoGP
# ---------------------------------------------------------------------------


def test_motogp_basic_parse() -> None:
    data = {
        "scores": {
            "tournament": [
                {
                    "id": "1326",
                    "name": "GP France",
                    "first_practice": {
                        "status": "Finished",
                        "results": {
                            "driver": [
                                {"driver_id": "1918", "name": "Luca Marini", "pos": "1", "team": "Honda HRC"},
                                {"driver_id": "1956", "name": "Pedro Acosta", "pos": "2", "team": "Red Bull KTM"},
                            ]
                        },
                    },
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("motogp", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == LiveEventKind.RACE
    assert ev.sport == "motogp"
    assert ev.status == SportsLiveGameStatus.ENDED
    assert len(ev.participants) == 2
    assert ev.participants[0].role == "driver"
    assert ev.participants[0].name == "Luca Marini"
    assert ev.participants[0].position == 1
    assert ev.participants[0].team == "Honda HRC"
    assert ev.source_event_id == "1326_first_practice"


def test_motogp_race_session_parse() -> None:
    data = {
        "scores": {
            "tournament": [
                {
                    "id": "1326",
                    "name": "GP France",
                    "race": {
                        "status": "In Progress",
                        "results": {
                            "driver": [{"driver_id": "1", "name": "Rider A", "pos": "1", "team": "Team X"}]
                        },
                    },
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("motogp", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status == SportsLiveGameStatus.LIVE
    assert events[0].source_event_id == "1326_race"


def test_f1_null_scores_returns_empty() -> None:
    # F1 between race weekends returns {"scores": null}
    assert parse_goalserve_livescore_sport("f1", {"scores": None}, observed_at=_OBSERVED) == []


def test_f1_race_session_parse() -> None:
    data = {
        "scores": {
            "tournament": [
                {
                    "id": "f1_100",
                    "name": "Monaco Grand Prix",
                    "race": {
                        "status": "Finished",
                        "laps_running": "78",
                        "total_laps": "78",
                        "results": {
                            "driver": [
                                {"driver_id": "d1", "name": "Max Verstappen", "pos": "1", "team": "Red Bull"},
                                {"driver_id": "d2", "name": "Charles Leclerc", "pos": "2", "team": "Ferrari"},
                            ]
                        },
                    },
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("f1", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == LiveEventKind.RACE
    assert ev.sport == "formula1"
    assert ev.status == SportsLiveGameStatus.ENDED
    assert ev.race_state is not None
    assert ev.race_state.laps_completed == 78
    assert ev.race_state.total_laps == 78
    assert len(ev.participants) == 2
    assert ev.participants[0].role == "driver"
    assert ev.participants[0].position == 1
    assert ev.source_event_id == "f1_100_race"


# ---------------------------------------------------------------------------
# as_payload() serialization
# ---------------------------------------------------------------------------


def test_as_payload_includes_new_states() -> None:
    data = _scores_team_wrap(
        {
            "id": "hb_p1",
            "status": "In Progress",
            "localteam": {"name": "A", "totalscore": "5"},
            "awayteam": {"name": "B", "totalscore": "3"},
        }
    )
    ev = parse_goalserve_livescore_sport("handball", data, observed_at=_OBSERVED)[0]
    payload = ev.as_payload()
    assert "handball_state" in payload
    assert "rugby_state" in payload
    assert "mma_state" in payload


def test_tennis_current_set_in_progress_first_set() -> None:
    """首盘进行中（5-2）时 current_set 必须是 1，不是 2。

    历史 bug：current_set = len(set_scores)+1，但 s{i} 字段在某盘进行中就有值，
    导致首盘进行中被算成第 2 盘 → set-winner 评估误判首盘已决出。
    """
    from polymarket_trader.infra.sports.goalserve_livescore_parsers import (
        _tennis_state_from_players,
    )

    st = _tennis_state_from_players([
        {"totalscore": "0", "s1": "5"},
        {"totalscore": "0", "s1": "2"},
    ])
    assert st is not None
    assert st.current_set == 1
    assert st.set_scores == ((5, 2),)
    assert st.home_current_set_games == 5


def test_tennis_current_set_advances_after_set_complete() -> None:
    from polymarket_trader.infra.sports.goalserve_livescore_parsers import (
        _tennis_state_from_players,
    )

    # 首盘 6-2 已完成、次盘 3-1 进行中 → current_set=2。
    st = _tennis_state_from_players([
        {"totalscore": "1", "s1": "6", "s2": "3"},
        {"totalscore": "0", "s1": "2", "s2": "1"},
    ])
    assert st is not None and st.current_set == 2

    # 首盘已完成、次盘未开始 → current_set=2（下一盘待开始）。
    st2 = _tennis_state_from_players([
        {"totalscore": "1", "s1": "6"},
        {"totalscore": "0", "s1": "2"},
    ])
    assert st2 is not None and st2.current_set == 2


# ---------------------------------------------------------------------------
# Volleyball — calibrated against real volleyball/home getfeed (2026-05-22)
# 真实结构：scores.category[].match[]，localteam/awayteam 有 totalscore + s1..s5。
# ---------------------------------------------------------------------------


def _volleyball_scores(*matches: dict, name: str = "World: Friendly International") -> dict:
    return {"scores": {"category": [{"name": name, "match": list(matches)}]}}


def test_volleyball_live_match_set_status() -> None:
    """真实 live 局：status "Set 2"，已打 1 盘（totalscore 1-0）+ 第 2 盘进行中。"""
    data = _volleyball_scores(
        {
            "id": "469996",
            "status": "Set 2",
            "time": "17:00",
            "localteam": {"name": "Croatia", "totalscore": "1", "s1": "26", "s2": "0",
                          "s3": "", "s4": "", "s5": ""},
            "awayteam": {"name": "Luxembourg", "totalscore": "0", "s1": "24", "s2": "0",
                         "s3": "", "s4": "", "s5": ""},
        }
    )
    events = parse_goalserve_livescore_sport("volleyball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.sport == "volleyball"
    assert ev.source == "goalserve_livescore"
    assert ev.kind is LiveEventKind.TEAM_MATCH
    assert ev.status is SportsLiveGameStatus.LIVE
    assert ev.league == "World: Friendly International"
    home, away = ev.participants
    assert (home.name, home.score) == ("Croatia", 1)
    assert (away.name, away.score) == ("Luxembourg", 0)
    st = ev.volleyball_state
    assert st is not None
    assert st.home_sets_won == 1 and st.away_sets_won == 0
    # 第 2 盘进行中（已打 2 盘 score，但只完成 1 盘）。
    assert st.current_set == 2
    assert st.set_scores == ((26, 24), (0, 0))


def test_volleyball_finished_match() -> None:
    """真实 finished 局：5 盘全打完，totalscore 3-2。"""
    data = _volleyball_scores(
        {
            "id": "469995",
            "status": "Finished",
            "localteam": {"name": "Austria", "totalscore": "3", "s1": "23", "s2": "25",
                          "s3": "25", "s4": "23", "s5": "15"},
            "awayteam": {"name": "Cyprus", "totalscore": "2", "s1": "25", "s2": "23",
                         "s3": "19", "s4": "25", "s5": "10"},
        }
    )
    events = parse_goalserve_livescore_sport("volleyball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.status is SportsLiveGameStatus.ENDED
    assert ev.participants[0].score == 3 and ev.participants[1].score == 2
    # ENDED 不构造 GameState（与其他 livescore parser 一致）。
    assert ev.volleyball_state is None


def test_volleyball_not_started() -> None:
    data = _volleyball_scores(
        {
            "id": "469968",
            "status": "Not Started",
            "localteam": {"name": "Poland", "totalscore": "", "s1": "", "s2": "",
                          "s3": "", "s4": "", "s5": ""},
            "awayteam": {"name": "Ukraine", "totalscore": "", "s1": "", "s2": "",
                         "s3": "", "s4": "", "s5": ""},
        }
    )
    events = parse_goalserve_livescore_sport("volleyball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    assert events[0].status is SportsLiveGameStatus.SCHEDULED
    assert events[0].participants[0].score is None


# ---------------------------------------------------------------------------
# American Football — calibrated against real football/home getfeed (2026-05-22)
# 真实结构：scores.category[].match（单场为 dict），localteam/awayteam.totalscore。
# ---------------------------------------------------------------------------


def test_amfootball_single_match_dict() -> None:
    """真实 football/home：某 category 只有一场时 match 是 dict（非 list）。"""
    data = {
        "scores": {
            "category": [
                {
                    "id": "1009",
                    "name": "Canada: Cfl - Pre-Season",
                    "match": {
                        "id": "129279",
                        "status": "Not Started",
                        "time": "23:00",
                        "timer": "",
                        "localteam": {"name": "Montreal Alouettes", "totalscore": ""},
                        "awayteam": {"name": "Ottawa Redblacks", "totalscore": ""},
                        "events": {"firstquarter": {"score": ""}},
                    },
                }
            ],
            "sport": "football",
        }
    }
    events = parse_goalserve_livescore_sport("amfootball", data, observed_at=_OBSERVED)
    assert len(events) == 1
    ev = events[0]
    assert ev.sport == "american-football"
    assert ev.source == "goalserve_livescore"
    assert ev.status is SportsLiveGameStatus.SCHEDULED
    assert ev.league == "Canada: Cfl - Pre-Season"
    assert ev.participants[0].name == "Montreal Alouettes"
    assert ev.participants[0].score is None


def test_amfootball_live_and_finished_scores() -> None:
    data = {
        "scores": {
            "category": [
                {
                    "id": "1162",
                    "name": "Usa: Af1",
                    "match": [
                        {
                            "id": "1",
                            "status": "3rd Quarter",
                            "timer": "5",
                            "localteam": {"name": "Minnesota Monsters", "totalscore": "17"},
                            "awayteam": {"name": "Nashville Kats", "totalscore": "14"},
                        },
                        {
                            "id": "2",
                            "status": "Finished",
                            "localteam": {"name": "DC Defenders", "totalscore": "28"},
                            "awayteam": {"name": "Orlando Storm", "totalscore": "21"},
                        },
                    ],
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("amfootball", data, observed_at=_OBSERVED)
    assert len(events) == 2
    live, done = events
    assert live.status is SportsLiveGameStatus.LIVE  # "3rd Quarter" → LIVE
    assert (live.participants[0].score, live.participants[1].score) == (17, 14)
    assert done.status is SportsLiveGameStatus.ENDED
    assert (done.participants[0].score, done.participants[1].score) == (28, 21)


def test_unknown_sport_returns_empty() -> None:
    assert parse_goalserve_livescore_sport("badminton", {"scores": {}}, observed_at=_OBSERVED) == []


# ---------------------------------------------------------------------------
# Config wiring — volleyball/amfootball must be reachable end-to-end
# ---------------------------------------------------------------------------


def test_sport_feeds_and_code_map_include_new_sports() -> None:
    """_SPORT_FEEDS 与 SPORT_CODE_TO_FEED_KEYS 都必须包含 volleyball/amfootball，
    否则 demand-driven 轮询永不抓取这两个 livescore 兜底源。"""
    from polymarket_trader.infra.sports.goalserve_livescore_client import (
        _SPORT_FEEDS,
        SPORT_CODE_TO_FEED_KEYS,
    )

    assert _SPORT_FEEDS["volleyball"] == ("volleyball/home", False)
    assert _SPORT_FEEDS["amfootball"] == ("football/home", False)
    # 规范运动码 → feed key 映射（american-football 是规范码，amfootball 是 feed key）。
    assert "amfootball" in SPORT_CODE_TO_FEED_KEYS["american-football"]
    assert "volleyball" in SPORT_CODE_TO_FEED_KEYS["volleyball"]
