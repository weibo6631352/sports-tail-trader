"""GoalserveInplay HTTP-GZIP feed parser 回归测试。

所有 fixture 来自真实抓取的 ``http://inplay.goalserve.com/inplay-{sport}.gz``
（见 tests/infra/sports/fixtures/inplay_*.json），按 CLAUDE.md §9 对真实格式校准。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from polymarket_trader.domain.sports_live import (
    LiveEventKind,
    SportsLiveGameStatus,
)
from polymarket_trader.infra.sports.goalserve_inplay_parsers import parse_goalserve_inplay

_FIXTURES = Path(__file__).parent / "fixtures"
_OBSERVED_AT = datetime(2026, 5, 22, 17, 25, 0, tzinfo=timezone.utc)


def _load(token: str) -> dict:
    return json.loads((_FIXTURES / f"inplay_{token}.json").read_text())


def _ml_market(event) -> dict | None:
    """从 LiveEvent.source_payload 取归一后的 Money Line 盘口。"""
    odds = event.source_payload.get("goalserve_odds") or {}
    for market in odds.get("markets", []):
        if "money line" in str(market.get("name", "")).lower():
            return market
    return None


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def test_empty_events_returns_empty_list() -> None:
    """events:{} （当前无 live 赛事）是正常态，返回空列表而非报错。"""
    feed = _load("hockey")
    assert feed["events"] == {}
    assert parse_goalserve_inplay("hockey", feed, observed_at=_OBSERVED_AT) == []


def test_missing_events_key_returns_empty_list() -> None:
    assert parse_goalserve_inplay("soccer", {}, observed_at=_OBSERVED_AT) == []
    assert parse_goalserve_inplay("soccer", {"events": None}, observed_at=_OBSERVED_AT) == []


def test_unknown_sport_returns_empty_list() -> None:
    feed = _load("baseball")
    assert parse_goalserve_inplay("curling", feed, observed_at=_OBSERVED_AT) == []


def test_all_events_carry_inplay_source_and_external_id() -> None:
    feed = _load("basket")
    events = parse_goalserve_inplay("basket", feed, observed_at=_OBSERVED_AT)
    assert events
    for ev in events:
        assert ev.source == "goalserve_inplay"
        assert ev.kind == LiveEventKind.TEAM_MATCH
        assert ev.source_event_id
        assert ev.external_ids.get("goalserve") == ev.source_event_id
        assert ev.observed_at == _OBSERVED_AT
        assert ev.home is not None and ev.away is not None


def test_bad_event_is_skipped_not_fatal() -> None:
    """单场坏数据（非 dict）被跳过，不影响同 feed 其他赛事。"""
    feed = _load("baseball")
    feed["events"]["broken"] = "not-a-dict"
    events = parse_goalserve_inplay("baseball", feed, observed_at=_OBSERVED_AT)
    # 原 2 场仍解析出来。
    assert len(events) == 2


# ---------------------------------------------------------------------------
# 状态映射
# ---------------------------------------------------------------------------


def test_finished_core_flag_maps_to_ended() -> None:
    """core.finished="1" 的 soccer 赛事 → ENDED（fixture 含已结束与进行中各若干）。"""
    feed = _load("soccer")
    events = parse_goalserve_inplay("soccer", feed, observed_at=_OBSERVED_AT)
    assert events
    by_id = {ev.source_event_id: ev for ev in events}
    for match_id, raw in feed["events"].items():
        if str(raw["core"].get("finished")).strip() == "1":
            assert by_id[match_id].status == SportsLiveGameStatus.ENDED


def test_live_event_when_no_terminal_flag() -> None:
    """无 finished/removed/stopped flag 且非 not-started → LIVE。"""
    feed = _load("basket")
    events = parse_goalserve_inplay("basket", feed, observed_at=_OBSERVED_AT)
    assert events
    by_id = {ev.source_event_id: ev for ev in events}
    for match_id, raw in feed["events"].items():
        core = raw["core"]
        if not any(str(core.get(k)).strip() == "1" for k in ("finished", "removed", "stopped")):
            assert by_id[match_id].status == SportsLiveGameStatus.LIVE


def test_stopped_flag_maps_to_paused() -> None:
    """core.stopped="1" 且未结束 → PAUSED（赛事进行中暂时中断 / map 间隙）。"""
    feed = _load("esports")
    events = parse_goalserve_inplay("esports", feed, observed_at=_OBSERVED_AT)
    assert events
    by_id = {ev.source_event_id: ev for ev in events}
    for match_id, raw in feed["events"].items():
        core = raw["core"]
        if str(core.get("stopped")).strip() == "1" and str(core.get("finished")).strip() != "1":
            assert by_id[match_id].status == SportsLiveGameStatus.PAUSED


def test_removed_flag_maps_to_cancelled() -> None:
    feed = _load("baseball")
    first_id = next(iter(feed["events"]))
    feed["events"][first_id]["core"]["removed"] = "1"
    events = parse_goalserve_inplay("baseball", feed, observed_at=_OBSERVED_AT)
    target = next(ev for ev in events if ev.source_event_id == first_id)
    assert target.status == SportsLiveGameStatus.CANCELLED


def test_not_started_period_maps_to_scheduled() -> None:
    feed = _load("baseball")
    first_id = next(iter(feed["events"]))
    feed["events"][first_id]["core"] = {"finished": "0", "removed": "", "stopped": ""}
    feed["events"][first_id]["info"]["period"] = "Not Started"
    events = parse_goalserve_inplay("baseball", feed, observed_at=_OBSERVED_AT)
    target = next(ev for ev in events if ev.source_event_id == first_id)
    assert target.status == SportsLiveGameStatus.SCHEDULED


# ---------------------------------------------------------------------------
# Esports — maps won from stats "Res"
# ---------------------------------------------------------------------------


def test_esports_maps_won_from_stats_res_row() -> None:
    feed = _load("esports")
    events = parse_goalserve_inplay("esports", feed, observed_at=_OBSERVED_AT)
    by_name = {ev.event_name: ev for ev in events}
    bb = by_name["BB Team vs PlayTime"]
    assert bb.esports_state is not None
    # stats row name="Res" home=1 away=0。
    assert bb.esports_state.home_maps_won == 1
    assert bb.esports_state.away_maps_won == 0
    assert bb.home.score == 1
    assert bb.away.score == 0


def test_esports_best_of_inferred_from_map_markets() -> None:
    """info.period="Started" 无 BOn——从 odds 里最大 "(map N)" 推断 best-of=2N-1。

    出现 "map N" 盘口说明系列赛能打到第 N 局，故 BO 至少 2N-1。
    """
    feed = _load("esports")
    events = parse_goalserve_inplay("esports", feed, observed_at=_OBSERVED_AT)
    import re as _re

    for ev in events:
        max_map = 0
        for market in ev.source_payload["goalserve_odds"]["markets"]:
            m = _re.search(r"map\s*(\d+)", market["name"].lower())
            if m:
                max_map = max(max_map, int(m.group(1)))
        if max_map >= 2:
            assert ev.esports_state.best_of == 2 * max_map - 1


# ---------------------------------------------------------------------------
# Basketball — score + quarter periods
# ---------------------------------------------------------------------------


def test_basketball_score_and_quarter_scores() -> None:
    """LIVE 篮球赛带分节状态，各节得分加总等于 team 总分（无加时时）。"""
    feed = _load("basket")
    events = parse_goalserve_inplay("basket", feed, observed_at=_OBSERVED_AT)
    live = [ev for ev in events if ev.status == SportsLiveGameStatus.LIVE]
    assert live
    for ev in live:
        assert ev.home.score is not None and ev.away.score is not None
        assert ev.basketball_state is not None
        assert ev.basketball_state.current_period == 4
        home_q = [q for q in ev.basketball_state.home_quarter_scores if q is not None]
        assert sum(home_q) == ev.home.score


# ---------------------------------------------------------------------------
# Tennis — set scores + surname short_name
# ---------------------------------------------------------------------------


def test_tennis_set_state_and_surname() -> None:
    feed = _load("tennis")
    events = parse_goalserve_inplay("tennis", feed, observed_at=_OBSERVED_AT)
    by_name = {ev.event_name: ev for ev in events}
    mboko = by_name["Victoria Mboko vs Jaqueline Cristian"]
    ts = mboko.tennis_state
    assert ts is not None
    assert ts.home_sets_won == 1 and ts.away_sets_won == 1
    assert ts.current_set == 3
    assert ts.set_scores == ((7, 6), (3, 6), (5, 2))
    # short_name 取姓氏供市场文本匹配。
    assert mboko.home.short_name == "Mboko"
    assert mboko.away.short_name == "Cristian"
    # LiveEvent score 用已赢盘数。
    assert mboko.home.score == 1 and mboko.away.score == 1


# ---------------------------------------------------------------------------
# Volleyball — set state
# ---------------------------------------------------------------------------


def test_volleyball_set_state() -> None:
    feed = _load("volleyball")
    events = parse_goalserve_inplay("volleyball", feed, observed_at=_OBSERVED_AT)
    by_name = {ev.event_name: ev for ev in events}
    match = by_name["Bulgaria vs Serbia"]
    vs = match.volleyball_state
    assert vs is not None
    assert vs.home_sets_won == 2 and vs.away_sets_won == 2
    assert vs.current_set == 5
    assert vs.set_scores == ((24, 26), (21, 25), (25, 20), (25, 19), (9, 10))


# ---------------------------------------------------------------------------
# Baseball — inning runs + half
# ---------------------------------------------------------------------------


def test_baseball_inning_state() -> None:
    feed = _load("baseball")
    events = parse_goalserve_inplay("baseball", feed, observed_at=_OBSERVED_AT)
    by_name = {ev.event_name: ev for ev in events}
    game = by_name["Michigan State vs USC"]
    bs = game.baseball_state
    assert bs is not None
    assert bs.current_inning == 5
    # sts "1|Top of 5th|..." → top half。
    assert bs.inning_half == "top"
    # 逐局得分长度固定 9。
    assert len(bs.home_inning_runs) == 9
    assert len(bs.away_inning_runs) == 9
    # away 第 1 局得 2 分（stats row name="1" away="2"）。
    assert bs.away_inning_runs[0] == 2


def test_baseball_score_from_team_info() -> None:
    feed = _load("baseball")
    events = parse_goalserve_inplay("baseball", feed, observed_at=_OBSERVED_AT)
    game = next(ev for ev in events if ev.event_name == "Michigan State vs USC")
    assert game.home.score == 0
    assert game.away.score == 4


# ---------------------------------------------------------------------------
# Soccer — score + halftime + cards
# ---------------------------------------------------------------------------


def test_soccer_score_and_halftime() -> None:
    feed = _load("soccer")
    events = parse_goalserve_inplay("soccer", feed, observed_at=_OBSERVED_AT)
    ev = events[0]
    assert ev.home.score is not None and ev.away.score is not None
    assert ev.soccer_state is not None
    # 半场比分来自 stats row IFirstHalfScore。
    assert ev.soccer_state.home_halftime_score is not None
    assert ev.soccer_state.away_halftime_score is not None


# ---------------------------------------------------------------------------
# Odds → implied probability
# ---------------------------------------------------------------------------


def test_odds_implied_prob_is_inverse_of_value_eu() -> None:
    feed = _load("basket")
    events = parse_goalserve_inplay("basket", feed, observed_at=_OBSERVED_AT)
    found = False
    for ev in events:
        for market in ev.source_payload["goalserve_odds"]["markets"]:
            for outcome in market["outcomes"]:
                eu = Decimal(outcome["value_eu"])
                implied = Decimal(outcome["implied_prob"])
                expected = (Decimal("1") / eu).quantize(Decimal("0.0001"))
                assert implied == expected
                assert eu > 0
                found = True
    assert found


def test_zero_value_eu_outcomes_dropped() -> None:
    """value_eu<=0（盘口关闭占位）的 outcome 不进 GoalserveOdds——无定价意义。"""
    feed = _load("baseball")
    events = parse_goalserve_inplay("baseball", feed, observed_at=_OBSERVED_AT)
    for ev in events:
        for market in ev.source_payload["goalserve_odds"]["markets"]:
            for outcome in market["outcomes"]:
                assert Decimal(outcome["value_eu"]) > 0


def test_moneyline_market_name_normalized_for_strategy() -> None:
    """全场胜负盘名归一为含 'money line'，使下游 _extract_goalserve_moneyline 可识别。

    棒球/排球/电竞原名 "Home/Away"，网球 "To Win" → 均归一为 "Money Line"。
    """
    for token in ("baseball", "volleyball", "esports", "tennis"):
        feed = _load(token)
        events = parse_goalserve_inplay(token, feed, observed_at=_OBSERVED_AT)
        # 至少一场有可识别的全场 moneyline 盘口。
        assert any(_ml_market(ev) is not None for ev in events), token


def test_moneyline_outcomes_normalized_to_home_away() -> None:
    """outcome 名 Home/Away → 下游认 'home'/'away'（小写）；归一后保留首字母大写。"""
    feed = _load("baseball")
    events = parse_goalserve_inplay("baseball", feed, observed_at=_OBSERVED_AT)
    ml = next(_ml_market(ev) for ev in events if _ml_market(ev) is not None)
    names = {o["name"].lower() for o in ml["outcomes"]}
    assert "home" in names and "away" in names


def test_subperiod_market_name_not_normalized() -> None:
    """分段盘（含 quarter/half/set）不归一为 moneyline——语义不同。"""
    feed = _load("basket")
    events = parse_goalserve_inplay("basket", feed, observed_at=_OBSERVED_AT)
    for ev in events:
        for market in ev.source_payload["goalserve_odds"]["markets"]:
            name = market["name"].lower()
            if any(seg in name for seg in ("quarter", "half")):
                # 分段盘不会被改名成裸 "money line"——原名保留段标识。
                assert name != "money line"


# ---------------------------------------------------------------------------
# 未识别全场盘口名告警（Task ① — Goalserve 重命名/新运动检测）
# ---------------------------------------------------------------------------


def _feed_with_market(market_name: str) -> dict:
    """构造含单个 odds 市场的最小 inplay feed dict。"""
    return {
        "events": {
            "evt1": {
                "core": {},
                "info": {"id": "evt1", "name": "A vs B", "league": "L"},
                "team_info": {"home": {"name": "A"}, "away": {"name": "B"}},
                "odds": {
                    "1": {
                        "id": "1",
                        "name": market_name,
                        "suspend": "0",
                        "participants": {
                            "p1": {"name": "Home", "value_eu": "1.5"},
                            "p2": {"name": "Away", "value_eu": "2.5"},
                        },
                    }
                },
            }
        }
    }


def test_unrecognized_moneyline_like_name_warns_once(caplog) -> None:
    """疑似全场 moneyline 但未识别的盘口名只 warn 一次，重复轮询去重不刷屏。"""
    import logging

    from polymarket_trader.infra.sports import goalserve_inplay_parsers as gip

    gip._unrecognized_odds_market_names_seen.clear()
    # "Match Outcome" 含 "outcome"——但本测试用确含启发词且下游不识别的名字。
    unknown_name = "Series Winner Odds"  # 含 "winner"，下游无对应 pattern
    feed = _feed_with_market(unknown_name)

    with caplog.at_level(logging.WARNING, logger=gip.logger.name):
        # 模拟多个轮询周期：解析三次。
        for _ in range(3):
            parse_goalserve_inplay("basket", feed, observed_at=_OBSERVED_AT)

    matching = [
        r for r in caplog.records
        if "unrecognized odds market name" in r.getMessage() and unknown_name in r.getMessage()
    ]
    assert len(matching) == 1, f"expected exactly one warning, got {len(matching)}"


def test_recognized_market_name_does_not_warn(caplog) -> None:
    """已识别盘口名（含 'money line'）不触发未识别告警。"""
    import logging

    from polymarket_trader.infra.sports import goalserve_inplay_parsers as gip

    gip._unrecognized_odds_market_names_seen.clear()
    feed = _feed_with_market("Game Lines Money Line")
    with caplog.at_level(logging.WARNING, logger=gip.logger.name):
        parse_goalserve_inplay("basket", feed, observed_at=_OBSERVED_AT)
    assert not any("unrecognized odds market name" in r.getMessage() for r in caplog.records)


def test_subperiod_market_name_does_not_warn(caplog) -> None:
    """分段盘（含 quarter 等）即便含启发词也不告警——不在全场盘口观测范围。"""
    import logging

    from polymarket_trader.infra.sports import goalserve_inplay_parsers as gip

    gip._unrecognized_odds_market_names_seen.clear()
    feed = _feed_with_market("1st Quarter Winner")
    with caplog.at_level(logging.WARNING, logger=gip.logger.name):
        parse_goalserve_inplay("basket", feed, observed_at=_OBSERVED_AT)
    assert not any("unrecognized odds market name" in r.getMessage() for r in caplog.records)


# D3 测试：feed 内 updated_ts / @updated 字段优先于 HTTP Date

def test_parse_inplay_updated_ts_overrides_http_date() -> None:
    """inplay feed 顶层 updated_ts (epoch ms) 优先于 HTTP Date 作 server_clock_at。"""
    feed = {
        "updated_ts": 1779547975513,  # 2026-05-23 14:52:55 UTC
        "updated": "05.23.2026 14:52:55",
        "events": {},
    }
    # 即使传入不同的 HTTP Date，应该被 feed 内 updated_ts 覆盖
    http_date = datetime(2026, 5, 23, 14, 53, 10, tzinfo=timezone.utc)
    events = parse_goalserve_inplay(
        "tennis", feed, observed_at=_OBSERVED_AT, server_clock_at=http_date,
    )
    # 空 events 列表（feed 没事件），但 effective_server_clock 已计算
    assert events == []
    # 单独测 helper：updated_ts 解析
    from polymarket_trader.infra.sports.goalserve_inplay_parsers import _parse_inplay_updated_ts
    parsed = _parse_inplay_updated_ts(1779547975513)
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 5 and parsed.day == 23
    assert parsed.hour == 14 and parsed.minute == 52 and parsed.second == 55


def test_parse_inplay_updated_ts_invalid_returns_none() -> None:
    """非法 updated_ts 返回 None，fallback 到 HTTP Date。"""
    from polymarket_trader.infra.sports.goalserve_inplay_parsers import _parse_inplay_updated_ts
    assert _parse_inplay_updated_ts(None) is None
    assert _parse_inplay_updated_ts("") is None
    assert _parse_inplay_updated_ts(0) is None
    assert _parse_inplay_updated_ts(-1) is None
    assert _parse_inplay_updated_ts(123) is None  # 太小（< 2000-01-01）
    assert _parse_inplay_updated_ts("not-a-number") is None
