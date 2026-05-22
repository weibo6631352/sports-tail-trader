"""板球（cricket）胜负盘扫尾评估回归测试。

板球胜负盘是 2-way MONEYLINE。锁定模型（追分局 chase）：追分方追平 target、
或所需 required_runs 超过剩余 balls×6、或追分方全员出局未达 target → 结果锁定。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from strategies.current.tail import (
    SportsMarketSnapshot,
    SportsMarketSide,
    SportsMarketType,
    TailRejectReason,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)
from strategies.current.tail.types import TailPolicy


def _game(
    *,
    status: str = "live",
    current_innings: int | None = 2,
    batting_side: str | None = "away",
    runs: int | None = None,
    wickets: int | None = None,
    target: int | None = None,
    required_runs: int | None = None,
    required_balls: int | None = None,
    home_score: int = 0,
    away_score: int = 0,
):
    return live_game_state_from_metadata({
        "league": "T20 Blast",
        "sport": "cricket",
        "home_name": "India",
        "away_name": "Australia",
        "home_score": home_score,
        "away_score": away_score,
        "period": "T20",
        "status": status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "cricket_state": {
            "current_innings": current_innings,
            "batting_side": batting_side,
            "runs": runs,
            "wickets": wickets,
            "target": target,
            "required_runs": required_runs,
            "required_balls": required_balls,
        },
    })


def _market(side: SportsMarketSide) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("20"),
        market_slug="t20-india-vs-australia-2026-05-23",
    )


def test_cricket_metadata_round_trip_rebuilds_state() -> None:
    game = _game(current_innings=2, batting_side="away", runs=180, target=251)
    assert game is not None
    assert game.cricket_state is not None
    assert game.cricket_state.target == 251
    assert game.cricket_state.batting_side == "away"


def test_cricket_chase_reached_locks_chasing_side() -> None:
    # 追分方（away）已追平 target → away 必胜。
    game = _game(batting_side="away", runs=252, target=251)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.AWAY), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "cricket_moneyline_chase_reached"


def test_cricket_chase_unreachable_locks_defending_side() -> None:
    # 追分方需 71 分但只剩 30 球（最多 180 分），仍可达；改用真正不可达场景：
    # 需 200 分剩 5 球（最多 30 分）→ 防守方（home）必胜。
    game = _game(
        batting_side="away", runs=50, target=251, required_runs=200, required_balls=5
    )
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "cricket_moneyline_chase_unreachable"


def test_cricket_all_out_below_target_locks_defending_side() -> None:
    # 追分方全员出局（10 wickets）且未达 target → 防守方（home）必胜。
    game = _game(batting_side="away", runs=200, wickets=10, target=251)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "cricket_moneyline_chase_all_out"


def test_cricket_first_innings_not_lockable() -> None:
    # 第 1 局（设定 target 的一方还在打）→ 尚无 target，不可锁定。
    game = _game(current_innings=1, batting_side="home", runs=120)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.CRICKET_NOT_LATE_ENOUGH.value


def test_cricket_missing_chase_data_precise_reject() -> None:
    # 追分局已开始但缺 target / required → 精确可审计拒绝，不臆测。
    game = _game(
        current_innings=2, batting_side="away", runs=100,
        target=None, required_runs=None, required_balls=None,
    )
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.AWAY), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.CRICKET_CHASE_DATA_MISSING.value


def test_cricket_chase_in_progress_not_locked() -> None:
    # 追分局进行中，required 数据齐全但结果尚未数学锁定。
    game = _game(
        batting_side="away", runs=120, target=251, required_runs=131, required_balls=60
    )
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.AWAY), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.CRICKET_OUTCOME_NOT_LOCKED.value


def test_cricket_missing_state_rejected() -> None:
    game = live_game_state_from_metadata({
        "league": "T20 Blast",
        "sport": "cricket",
        "home_name": "India",
        "away_name": "Australia",
        "home_score": 0,
        "away_score": 0,
        "period": "T20",
        "status": "live",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.MISSING_CRICKET_STATE.value


def test_cricket_ended_winner_accepted() -> None:
    # 比赛结束 → participant.score（胜者 1）经 ended-moneyline 锁定胜方。
    game = _game(status="ended", home_score=1, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "ended_not_closed_moneyline"
