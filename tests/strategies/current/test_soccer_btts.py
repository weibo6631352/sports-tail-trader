"""足球 BTTS（both teams to score，双方进球）盘口评估回归测试。

双方均已进球 → BTTS YES 100% 锁定（进球既成事实、不可撤销）。NO 侧在比赛
结束前无法锁定（0 球方随时可能进球）。
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


def _market(side: SportsMarketSide) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug="j1100-zel-ura-2026-05-22-btts",
    )


def test_btts_yes_locked_when_both_scored() -> None:
    game = _game(home_score=2, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "soccer_btts_yes_locked"


def test_btts_yes_rejected_when_only_home_scored() -> None:
    game = _game(home_score=1, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


def test_btts_yes_rejected_when_goalless() -> None:
    game = _game(home_score=0, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES), policy=TailPolicy())
    assert not ev.accepted


def test_btts_no_not_lockable_live() -> None:
    # NO 在比赛结束前不可锁定——0 球方随时可能进球。
    game = _game(home_score=1, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.NO), policy=TailPolicy())
    assert not ev.accepted
