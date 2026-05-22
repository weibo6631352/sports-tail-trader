"""足球胜负盘（3-way 拆成的 binary_prop）评估回归测试。

足球进球稀少：临近终场领先 2-3 球即基本锁定，所需净胜球按剩余时间缩放。
平局盘中不可锁定。
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
        "league": "J1 League",
        "sport": "soccer",
        "home_name": "Machida Zelvia",
        "away_name": "Urawa Reds",
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
        market_slug=f"j1100-zel-ura-2026-05-22-{suffix}",
    )


def test_soccer_home_win_locked_on_three_goal_late_lead() -> None:
    # 主队领先 3、剩 10 分钟（所需 3）→ "Zelvia win" YES 锁定。
    game = _game(home_score=3, away_score=0, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "zel"), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "soccer_moneyline_locked"


def test_soccer_home_win_rejected_when_lead_too_small() -> None:
    # 领先 2、剩 10 分钟（所需 3）→ 拒绝。
    game = _game(home_score=2, away_score=0, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "zel"), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.INSUFFICIENT_LEAD.value


def test_soccer_home_win_locked_near_fulltime_two_goal_lead() -> None:
    # 领先 2、剩 3 分钟（所需 2）→ 锁定。
    game = _game(home_score=2, away_score=0, seconds_remaining=180)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "zel"), policy=TailPolicy())
    assert ev.accepted, ev.reason


def test_soccer_rejected_when_too_early() -> None:
    game = _game(home_score=3, away_score=0, seconds_remaining=1800)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "zel"), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.GAME_NOT_LATE_ENOUGH.value


def test_soccer_home_win_no_locked_when_home_losing_big() -> None:
    game = _game(home_score=0, away_score=3, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.NO, "zel"), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "soccer_moneyline_no_locked"


def test_soccer_away_win_locked() -> None:
    game = _game(home_score=0, away_score=3, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "ura"), policy=TailPolicy())
    assert ev.accepted, ev.reason


def test_soccer_draw_market_not_lockable() -> None:
    game = _game(home_score=3, away_score=0, seconds_remaining=600)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.YES, "draw"), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value
