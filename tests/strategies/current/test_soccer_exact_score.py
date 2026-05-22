"""足球精确比分（exact-score）盘口评估回归测试。

比分只增不减：现有比分一旦在任一方向超过目标 → 该精确比分永不可能 → NO 锁定。
YES 仅在终场比分恰好等于目标时成立，盘中不可锁定。
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


def _game(home_score: int, away_score: int):
    return live_game_state_from_metadata({
        "league": "J1 League",
        "sport": "soccer",
        "home_name": "Machida Zelvia",
        "away_name": "Urawa Reds",
        "home_score": home_score,
        "away_score": away_score,
        "period": "second_half",
        "status": "live",
        "seconds_remaining": 600,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _market(side: SportsMarketSide, target: str) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug=f"j1100-zel-ura-2026-05-22-exact-score-{target}",
    )


def test_exact_score_no_locked_when_home_passed_target() -> None:
    # 目标 2-1，现 3-1：主队已超 2 → 终场永不可能 2-1 → NO 锁定。
    game = _game(home_score=3, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.NO, "2-1"), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "soccer_exact_score_no_locked"


def test_exact_score_no_locked_when_away_passed_target() -> None:
    # 目标 2-1，现 2-2：客队已超 1 → NO 锁定。
    game = _game(home_score=2, away_score=2)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.NO, "2-1"), policy=TailPolicy())
    assert ev.accepted, ev.reason


def test_exact_score_no_rejected_when_still_possible() -> None:
    # 目标 2-1，现 1-1：仍可能到 2-1 → NO 不锁定。
    game = _game(home_score=1, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.NO, "2-1"), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


def test_exact_score_yes_not_lockable_live() -> None:
    # 目标 2-1，现恰好 2-1：盘中比分仍可变 → YES 不可锁定。
    game = _game(home_score=2, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "2-1"), policy=TailPolicy())
    assert not ev.accepted
