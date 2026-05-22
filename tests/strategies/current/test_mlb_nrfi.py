"""MLB NRFI（首局无得分）盘口评估回归测试。

NRFI 是 Polymarket MLB 一场比赛里唯一的非整场特殊盘口（binary_prop）。
NRFI=Yes：首局双方均 0 分。NRFI=No：首局有任意得分。
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


def _game(current_inning: int, home_in: list, away_in: list):
    return live_game_state_from_metadata({
        "league": "MLB",
        "sport": "baseball",
        "home_name": "Arizona Diamondbacks",
        "away_name": "Colorado Rockies",
        "home_score": sum(x for x in home_in if x),
        "away_score": sum(x for x in away_in if x),
        "period": f"Inning {current_inning}",
        "status": "live",
        "seconds_remaining": 1800,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "baseball_state": {
            "current_inning": current_inning,
            "inning_half": "top",
            "home_inning_runs": home_in,
            "away_inning_runs": away_in,
        },
    })


def _nrfi(side: SportsMarketSide) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug="mlb-ari-col-2026-05-22-nrfi",
    )


def test_nrfi_no_locked_when_run_scored_first_inning() -> None:
    # 首局已有得分 → NRFI No 锁定（即使首局还没结束也算）。
    game = _game(current_inning=1, home_in=[2], away_in=[None])
    assert game is not None
    ev = evaluate_tail_opportunity(game, _nrfi(SportsMarketSide.NO), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "mlb_nrfi_no_locked"


def test_nrfi_yes_rejected_when_run_scored() -> None:
    game = _game(current_inning=2, home_in=[1], away_in=[0])
    assert game is not None
    ev = evaluate_tail_opportunity(game, _nrfi(SportsMarketSide.YES), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


def test_nrfi_yes_locked_when_first_inning_scoreless_and_complete() -> None:
    # 首局结束（已到第 2 局）且双方首局 0 分 → NRFI Yes 锁定。
    game = _game(current_inning=2, home_in=[0], away_in=[0])
    assert game is not None
    ev = evaluate_tail_opportunity(game, _nrfi(SportsMarketSide.YES), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "mlb_nrfi_yes_locked"


def test_nrfi_yes_rejected_when_first_inning_not_complete() -> None:
    # 首局进行中、暂时 0 分 → 未锁定，不能买 Yes。
    game = _game(current_inning=1, home_in=[0], away_in=[None])
    assert game is not None
    ev = evaluate_tail_opportunity(game, _nrfi(SportsMarketSide.YES), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.BASEBALL_FIRST_INNING_NOT_COMPLETE.value


def test_nrfi_yes_rejected_when_inning_runs_data_missing() -> None:
    # 首局结束但首局得分数据缺失 → 不能确认 0 分，拒绝。
    game = _game(current_inning=3, home_in=[], away_in=[])
    assert game is not None
    ev = evaluate_tail_opportunity(game, _nrfi(SportsMarketSide.YES), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.MISSING_BASEBALL_STATE.value
