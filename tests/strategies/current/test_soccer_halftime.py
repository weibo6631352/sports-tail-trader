"""足球半场赛果（halftime-result）盘口评估回归测试。

数据源在半场结束后才给出 <ht> 比分；两侧半场比分齐全即半场已锁定、赛果确定。
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


def _game(ht_home: int | None, ht_away: int | None):
    soccer_state = None
    if ht_home is not None and ht_away is not None:
        # period=second_half 必须在 soccer_state 内(parser 1st half 默认填 0 + period
        # 区分);evaluator 已守卫:必须 period 进入 second_half/ended 才认半场锁定。
        soccer_state = {
            "home_halftime_score": ht_home,
            "away_halftime_score": ht_away,
            "period": "second_half",
        }
    return live_game_state_from_metadata({
        "league": "J1 League",
        "sport": "soccer",
        "home_name": "Machida Zelvia",
        "away_name": "Urawa Reds",
        "home_score": 2,
        "away_score": 1,
        "period": "second_half",
        "status": "live",
        "seconds_remaining": 600,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "soccer_state": soccer_state,
    })


def _market(side: SportsMarketSide, direction: str) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug=f"j1100-zel-ura-2026-05-22-halftime-result-{direction}",
    )


def test_halftime_home_yes_locked() -> None:
    game = _game(ht_home=1, ht_away=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "home"), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "soccer_halftime_result_locked"


def test_halftime_home_yes_rejected_when_draw() -> None:
    game = _game(ht_home=0, ht_away=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "home"), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


def test_halftime_draw_yes_locked() -> None:
    game = _game(ht_home=0, ht_away=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "draw"), policy=TailPolicy())
    assert ev.accepted, ev.reason


def test_halftime_away_yes_locked() -> None:
    game = _game(ht_home=1, ht_away=2)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "away"), policy=TailPolicy())
    assert ev.accepted, ev.reason


def test_halftime_home_no_locked_when_draw() -> None:
    # 半场 0-0：home 没赢 → halftime-result-home 的 No 锁定。
    game = _game(ht_home=0, ht_away=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.NO, "home"), policy=TailPolicy())
    assert ev.accepted, ev.reason


def test_halftime_rejected_before_halftime() -> None:
    # 半场未结束（无 <ht> 比分）→ 不能锁定。
    game = _game(ht_home=None, ht_away=None)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "home"), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.SOCCER_HALFTIME_NOT_COMPLETE.value
