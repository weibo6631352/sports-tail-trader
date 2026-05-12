"""Phase B 三个新源 client 的 golden fixture 解析测试。

每个 fixture 跑一次 parse_*_payload，验证关键归一化字段（kind/sport/league/
participants/scores/external_ids/event_start_time）符合内部 LiveEvent 契约。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from polymarket_trader.domain.sports_live import LiveEventKind, SportsLiveGameStatus
from polymarket_trader.infra.sports import (
    parse_api_football_payload,
    parse_cfbd_games_payload,
    parse_ncaa_api_scoreboard,
    parse_tennis_live_data_payload,
)


_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "sports_live"
_OBSERVED = datetime(2026, 5, 25, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text())


def test_tennis_live_data_parser_normalizes_atp_match() -> None:
    payload = _load("tennis_live_data/atp_sample.json")
    events = parse_tennis_live_data_payload(payload, observed_at=_OBSERVED)
    assert len(events) == 1
    event = events[0]
    assert event.kind == LiveEventKind.TEAM_MATCH
    assert event.sport == "tennis"
    assert event.league == "ATP"
    assert event.source == "tennis_live_data"
    assert event.source_event_id == "12345"
    home = event.home
    away = event.away
    assert home is not None and away is not None
    assert home.name == "Carlos Alcaraz"
    assert away.name == "Jannik Sinner"
    # external_ids 同时填了 tennis_live_data + atp_id（跨源合并关键）
    assert home.external_ids.get("tennis_live_data") == "alcaraz-1"
    assert home.external_ids.get("atp_id") == "207989"
    assert away.external_ids.get("atp_id") == "206173"
    assert event.event_external_ids_includes_self() if False else event.external_ids.get("tennis_live_data") == "12345"
    # tennis_state 解析了 3 个 set；前两个分一胜一负 → home_sets_won=1 away_sets_won=1
    assert event.tennis_state is not None
    assert event.tennis_state.home_sets_won == 1
    assert event.tennis_state.away_sets_won == 1
    assert event.status == SportsLiveGameStatus.LIVE


def test_api_football_parser_normalizes_live_fixture() -> None:
    payload = _load("api_football/epl_sample.json")
    events = parse_api_football_payload(payload, observed_at=_OBSERVED)
    assert len(events) == 1
    event = events[0]
    assert event.kind == LiveEventKind.TEAM_MATCH
    assert event.sport == "football"
    assert event.league == "Premier League"
    assert event.source == "api_football"
    assert event.source_event_id == "998877"
    assert event.external_ids.get("api_football") == "998877"
    assert event.home is not None and event.away is not None
    assert event.home.score == 1
    assert event.away.score == 2
    assert event.status == SportsLiveGameStatus.LIVE
    assert event.soccer_state is not None
    assert event.soccer_state.clock_minutes == 67
    assert event.soccer_state.period == "second_half"


def test_cfbd_parser_normalizes_ncaaf_game() -> None:
    payload = _load("cfbd/ncaaf_sample.json")
    events = parse_cfbd_games_payload(payload, observed_at=_OBSERVED)
    assert len(events) == 1
    event = events[0]
    assert event.sport == "american-football"
    assert event.league == "NCAAF"
    assert event.source == "college_football_data"
    assert event.external_ids.get("college_football_data") == "401601001"
    assert event.home is not None and event.away is not None
    assert event.home.name == "Alabama"
    assert event.home.score == 21
    assert event.away.score == 14
    # 已有分数但 completed=false → 视为 LIVE
    assert event.status == SportsLiveGameStatus.LIVE


def test_ncaa_api_parser_normalizes_ncaab_game() -> None:
    payload = _load("ncaa_api/ncaab_sample.json")
    events = parse_ncaa_api_scoreboard(payload, observed_at=_OBSERVED)
    assert len(events) == 1
    event = events[0]
    assert event.sport == "basketball"
    assert event.league == "NCAAB"
    assert event.source == "ncaa_api"
    assert event.external_ids.get("ncaa") == "5859812"
    assert event.home is not None and event.away is not None
    assert event.home.name == "Duke"
    assert event.away.name == "North Carolina"
    assert event.home.score == 42
    assert event.away.score == 38
    assert event.status == SportsLiveGameStatus.LIVE
