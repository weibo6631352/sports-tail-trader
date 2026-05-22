"""Anytime-goalscorer 扫尾评估回归测试。

覆盖：球员名称匹配（HIGH / MEDIUM / 重音归一化）、parser 提取进球事件、
评估器锁定 YES / 锁定 NO / 进行中不锁定 / 错方向拒绝、ENDED 与 LIVE
路径路由都生效。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.sports_live import (
    SoccerGameState,
    SoccerGoalEvent,
    SportsLiveGameStatus,
)
from polymarket_trader.infra.sports.goalserve_livescore_parsers import (
    _extract_soccer_goal_events,
    parse_goalserve_livescore_sport,
)
from strategies.current.tail import (
    SportsMarketSnapshot,
    SportsMarketSide,
    SportsMarketType,
    TailRejectReason,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)
from strategies.current.tail.anytime_goalscorer import (
    MatchConfidence,
    _slug_player_matches_goal_event,
    extract_slug_player_name,
    is_anytime_goalscorer_market,
)
from strategies.current.tail.types import TailPolicy


# ---- 球员名称匹配 ----------------------------------------------------------


def _goal(player_name: str = "M. Pasalic", team: str = "home") -> SoccerGoalEvent:
    return SoccerGoalEvent(
        player_name=player_name,
        player_id="999",
        team=team,  # type: ignore[arg-type]
        minute=23,
        score_after="[1 - 0]",
    )


def test_player_match_full_name_high_confidence() -> None:
    assert (
        _slug_player_matches_goal_event("mario-pasalic", _goal("M. Pasalic"))
        == MatchConfidence.HIGH
    )


def test_player_match_lastname_only_slug_medium() -> None:
    assert (
        _slug_player_matches_goal_event("pasalic", _goal("M. Pasalic"))
        == MatchConfidence.MEDIUM
    )


def test_player_match_diacritics_normalized_high() -> None:
    # Goalserve 可能给 "Raúl" 或 "Raul"；slug 一律 ASCII。归一化后应等价。
    assert (
        _slug_player_matches_goal_event("raul-garcia", _goal("R. García"))
        == MatchConfidence.HIGH
    )


def test_player_match_different_lastname_low() -> None:
    assert (
        _slug_player_matches_goal_event("mario-pasalic", _goal("L. Messi"))
        == MatchConfidence.LOW
    )


def test_player_match_initial_mismatch_low() -> None:
    # 同姓不同人——首字母都给但不一致 → LOW。
    assert (
        _slug_player_matches_goal_event("john-pasalic", _goal("M. Pasalic"))
        == MatchConfidence.LOW
    )


# ---- 识别 -----------------------------------------------------------------


def _ags_market(
    side: SportsMarketSide,
    *,
    slug: str = "epl-ars-tot-2026-05-22-ags-mario-pasalic",
    sports_market_type: str = "soccer_anytime_goalscorer",
    best_ask: Decimal | None = Decimal("0.50"),
) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=side,
        token_id="tok",
        line=None,
        best_ask=best_ask,
        buyable_liquidity_usdc=Decimal("100"),
        market_slug=slug,
        sports_market_type=sports_market_type,
    )


def test_recognizer_by_sports_market_type() -> None:
    market = _ags_market(SportsMarketSide.YES, slug="random-slug-no-marker")
    assert is_anytime_goalscorer_market(market) is True


def test_recognizer_by_slug_ags_segment() -> None:
    market = _ags_market(SportsMarketSide.YES, sports_market_type="")
    assert is_anytime_goalscorer_market(market) is True
    assert extract_slug_player_name(market) == "mario-pasalic"


def test_recognizer_rejects_unrelated_slug() -> None:
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=SportsMarketSide.YES,
        token_id="x",
        line=None,
        best_ask=Decimal("0.5"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug="epl-ars-tot-2026-05-22-btts",
    )
    assert is_anytime_goalscorer_market(market) is False


# ---- 评估器：LIVE 路径 ----------------------------------------------------


def _live_game(
    *,
    home_score: int = 1,
    away_score: int = 0,
    status: str = "live",
    goal_events: tuple[SoccerGoalEvent, ...] = (),
):
    soccer_state = SoccerGameState(goal_events=goal_events)
    return live_game_state_from_metadata({
        "league": "EPL",
        "sport": "soccer",
        "home_name": "Arsenal",
        "away_name": "Tottenham",
        "home_score": home_score,
        "away_score": away_score,
        "period": "second_half",
        "status": status,
        "seconds_remaining": 600,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "soccer_state": {
            "goal_events": [
                {
                    "player_name": g.player_name,
                    "player_id": g.player_id,
                    "team": g.team,
                    "minute": g.minute,
                    "score_after": g.score_after,
                }
                for g in soccer_state.goal_events
            ],
        },
    })


def test_yes_locked_when_player_scored() -> None:
    game = _live_game(
        home_score=1,
        away_score=0,
        goal_events=(_goal("M. Pasalic", team="home"),),
    )
    assert game is not None
    ev = evaluate_tail_opportunity(game, _ags_market(SportsMarketSide.YES), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason.startswith("anytime_goalscorer_yes_locked")


def test_no_rejected_wrong_side_when_player_scored() -> None:
    game = _live_game(
        home_score=1,
        away_score=0,
        goal_events=(_goal("M. Pasalic", team="home"),),
    )
    assert game is not None
    ev = evaluate_tail_opportunity(game, _ags_market(SportsMarketSide.NO), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ANYTIME_GOALSCORER_WRONG_SIDE.value


def test_in_progress_no_goal_not_locked() -> None:
    game = _live_game(home_score=0, away_score=0, goal_events=())
    assert game is not None
    ev = evaluate_tail_opportunity(game, _ags_market(SportsMarketSide.YES), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


def test_ambiguous_player_name_rejected() -> None:
    # slug 不含可识别的 ags 球员段——recognizer 由 sports_market_type 命中，
    # 但 extract_slug_player_name 解析失败 → 精确拒绝。
    market = _ags_market(SportsMarketSide.YES, slug="unrelated-slug")
    game = _live_game(goal_events=(_goal(),))
    assert game is not None
    ev = evaluate_tail_opportunity(game, market, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ANYTIME_GOALSCORER_PLAYER_AMBIGUOUS.value


# ---- 评估器：ENDED 路径 ---------------------------------------------------


def test_no_locked_when_game_ended_player_never_scored() -> None:
    # 比赛已结束，进球流非空（确认 feed 在线），但 Pasalic 不在进球者列表里。
    game = _live_game(
        home_score=2,
        away_score=1,
        status="ended",
        goal_events=(
            _goal("L. Messi", team="home"),
            _goal("S. Aguero", team="home"),
            _goal("H. Kane", team="away"),
        ),
    )
    assert game is not None
    ev = evaluate_tail_opportunity(game, _ags_market(SportsMarketSide.NO), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "anytime_goalscorer_no_locked"


def test_yes_rejected_wrong_side_when_ended_without_player_goal() -> None:
    game = _live_game(
        home_score=2,
        away_score=1,
        status="ended",
        goal_events=(
            _goal("L. Messi", team="home"),
            _goal("S. Aguero", team="home"),
            _goal("H. Kane", team="away"),
        ),
    )
    assert game is not None
    ev = evaluate_tail_opportunity(game, _ags_market(SportsMarketSide.YES), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ANYTIME_GOALSCORER_WRONG_SIDE.value


def test_ended_with_score_but_empty_goal_feed_rejected_missing_state() -> None:
    # 比赛已结束、有进球但 goal_events 为空——直播 feed 链路失败，不可假阳性锁 NO。
    game = _live_game(home_score=2, away_score=1, status="ended", goal_events=())
    assert game is not None
    ev = evaluate_tail_opportunity(game, _ags_market(SportsMarketSide.NO), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.MISSING_LIVE_GAME_STATE.value


# ---- Parser：从 livescore feed 提取进球事件 -------------------------------


def _real_match_dict() -> dict:
    """对照真实 Goalserve XML→dict 结构（events 用 _children 列表）。"""
    return {
        "id": "M1",
        "status": "FT",
        "localteam": {"name": "USM Alger", "goals": "1"},
        "visitorteam": {"name": "Paradou", "goals": "0"},
        "events": {
            "_children": [
                {
                    "type": "goal",
                    "minute": "23",
                    "team": "localteam",
                    "player": "A. Khaldi",
                    "playerId": "1133677",
                    "result": "[1 - 0]",
                    "_tag": "event",
                },
                {
                    "type": "yellowcard",
                    "minute": "30",
                    "team": "visitorteam",
                    "player": "Some Player",
                    "playerId": "10",
                    "_tag": "event",
                },
                {
                    "type": "goal",
                    "minute": "55",
                    "team": "visitorteam",
                    "player": "M. Pasalic",
                    "playerId": "2000",
                    "result": "[1 - 1]",
                    "_tag": "event",
                },
            ]
        },
    }


def test_extract_goal_events_filters_non_goal_and_maps_team() -> None:
    goals = _extract_soccer_goal_events(_real_match_dict())
    assert len(goals) == 2
    assert goals[0].player_name == "A. Khaldi"
    assert goals[0].team == "home"
    assert goals[0].minute == 23
    assert goals[0].score_after == "[1 - 0]"
    assert goals[1].player_name == "M. Pasalic"
    assert goals[1].team == "away"


def test_parser_populates_soccer_state_goal_events() -> None:
    payload = {
        "scores": {
            "category": [
                {
                    "name": "Algeria League",
                    "match": [_real_match_dict()],
                }
            ]
        }
    }
    events = parse_goalserve_livescore_sport("soccer", payload, observed_at=datetime.now(timezone.utc))
    assert len(events) == 1
    ev = events[0]
    assert ev.status == SportsLiveGameStatus.ENDED
    assert ev.soccer_state is not None
    assert len(ev.soccer_state.goal_events) == 2
    assert {g.player_id for g in ev.soccer_state.goal_events} == {"1133677", "2000"}


def test_extract_goal_events_handles_json_form_with_at_prefix() -> None:
    # ?json=1 形态：events.event 列表，key 带 @ 前缀。
    match = {
        "events": {
            "event": [
                {
                    "@type": "goal",
                    "@minute": "10",
                    "@team": "localteam",
                    "@player": "X. Player",
                    "@playerId": "1",
                    "@result": "[1 - 0]",
                }
            ]
        }
    }
    goals = _extract_soccer_goal_events(match)
    assert len(goals) == 1
    assert goals[0].player_name == "X. Player"
    assert goals[0].team == "home"
