"""橄榄球胜负盘评估回归测试。

橄榄球 Polymarket 盘口为 3-way 拆成的 binary_prop（slug 后缀 = 主队缩写 /
draw / 客队缩写）。胜负在比分差足够大且临近终场时锁定，所需领先按剩余时间缩放。
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


def _game(home_score: int, away_score: int, seconds_remaining: int | None):
    return live_game_state_from_metadata({
        "league": "Super Rugby Pacific",
        "sport": "rugby",
        "home_name": "Waratahs",
        "away_name": "Brumbies",
        "home_score": home_score,
        "away_score": away_score,
        "period": "second_half",
        "status": "live",
        "seconds_remaining": seconds_remaining,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _market(side: SportsMarketSide, suffix: str) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("20"),
        market_slug=f"rusrp-war-bru-2026-05-22-{suffix}",
    )


def test_rugby_home_win_locked_on_big_late_lead() -> None:
    # 主队领先 22、剩 10 分钟（所需 15）→ "Waratahs win" YES 锁定。
    game = _game(home_score=30, away_score=8, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "war"), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "rugby_moneyline_locked"


def test_rugby_home_win_rejected_when_lead_too_small() -> None:
    # 领先仅 5、剩 10 分钟（所需 15）→ 拒绝。
    game = _game(home_score=20, away_score=15, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "war"), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.RUGBY_LEAD_NOT_SAFE.value


def test_rugby_rejected_when_too_early() -> None:
    # 剩 30 分钟（>20）→ 太早，不锁定。
    game = _game(home_score=30, away_score=8, seconds_remaining=1800)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "war"), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.RUGBY_NOT_LATE_ENOUGH.value


def test_rugby_home_win_no_locked_when_home_losing_big() -> None:
    # 主队落后 22、剩 10 分钟 → "Waratahs win" NO 锁定（主队已不可能赢）。
    game = _game(home_score=8, away_score=30, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.NO, "war"), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "rugby_moneyline_no_locked"


def test_rugby_away_win_locked() -> None:
    # 客队领先 22 → "Brumbies win" YES 锁定。
    game = _game(home_score=8, away_score=30, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "bru"), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "rugby_moneyline_locked"


def test_rugby_draw_market_not_supported() -> None:
    game = _game(home_score=30, away_score=8, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "draw"), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.RUGBY_DRAW_NOT_SUPPORTED.value


def test_rugby_near_fulltime_smaller_lead_locks() -> None:
    # 剩 4 分钟（所需仅 9）、领先 10 → 锁定（临近终场所需领先更小）。
    game = _game(home_score=22, away_score=12, seconds_remaining=240)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "war"), policy=TailPolicy())
    assert ev.accepted, ev.reason
