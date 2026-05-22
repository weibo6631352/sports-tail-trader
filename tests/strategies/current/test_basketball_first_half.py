"""篮球上半场（1H）盘口评估回归测试。

1H total / spread / moneyline 此前作为 unsupported_period 被拒。半场结束
（进入第 3 节）后，1H 结果由第 1、2 节得分 100% 锁定，是干净的扫尾盘口。
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


def _game(current_period: int, home_q: list, away_q: list):
    return live_game_state_from_metadata({
        "league": "NBA",
        "sport": "basketball",
        "home_name": "New York Knicks",
        "away_name": "Cleveland Cavaliers",
        "home_score": sum(x for x in home_q if x),
        "away_score": sum(x for x in away_q if x),
        "period": f"Q{current_period}",
        "status": "live",
        "seconds_remaining": 600,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "basketball_state": {
            "current_period": current_period,
            "home_quarter_scores": home_q,
            "away_quarter_scores": away_q,
        },
    })


def _market(
    market_type: SportsMarketType,
    side: SportsMarketSide,
    line: str | None,
    slug_suffix: str,
) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=market_type,
        side=side,
        token_id="test",
        line=None if line is None else Decimal(line),
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug=f"nba-nyk-cle-2026-05-22-{slug_suffix}",
    )


def test_1h_total_over_locked_after_halftime() -> None:
    # Q1+Q2 = (30+28)+(25+24) = 107 > 104.5 → Over 锁定。
    game = _game(current_period=3, home_q=[30, 28, None, None], away_q=[25, 24, None, None])
    assert game is not None
    m = _market(SportsMarketType.TOTALS, SportsMarketSide.OVER, "104.5", "1h-total-104pt5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "basketball_1h_total_over_locked"


def test_1h_total_under_locked_after_halftime() -> None:
    # 1H total 107 < 110.5 → Under 锁定。
    game = _game(current_period=4, home_q=[30, 28, 20, None], away_q=[25, 24, 22, None])
    assert game is not None
    m = _market(SportsMarketType.TOTALS, SportsMarketSide.UNDER, "110.5", "1h-total-110pt5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "basketball_1h_total_under_locked"


def test_1h_not_locked_before_halftime() -> None:
    # 还在第 2 节 → 上半场未结束，不能锁定。
    game = _game(current_period=2, home_q=[30, None, None, None], away_q=[25, None, None, None])
    assert game is not None
    m = _market(SportsMarketType.TOTALS, SportsMarketSide.OVER, "104.5", "1h-total-104pt5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.BASKETBALL_FIRST_HALF_NOT_COMPLETE.value


def test_1h_moneyline_locked_after_halftime() -> None:
    # 1H：home 58 vs away 49 → home 1H moneyline 锁定。
    game = _game(current_period=3, home_q=[30, 28, None, None], away_q=[25, 24, None, None])
    assert game is not None
    m = _market(SportsMarketType.MONEYLINE, SportsMarketSide.HOME, None, "1h-moneyline")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "basketball_1h_moneyline_locked"


def test_1h_moneyline_losing_side_rejected() -> None:
    game = _game(current_period=3, home_q=[30, 28, None, None], away_q=[25, 24, None, None])
    assert game is not None
    m = _market(SportsMarketType.MONEYLINE, SportsMarketSide.AWAY, None, "1h-moneyline")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    # 上半场未锁定 + 无 Goalserve 赔率差价 → Money Line 落到 no_odds_gap。
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


def test_1h_spread_covered_after_halftime() -> None:
    # 1H margin home +9；spread home -3.5 → 9 + (-3.5) = 5.5 > 0 → 锁定覆盖。
    game = _game(current_period=3, home_q=[30, 28, None, None], away_q=[25, 24, None, None])
    assert game is not None
    m = _market(SportsMarketType.SPREADS, SportsMarketSide.HOME, "-3.5", "1h-spread-home-minus-3pt5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "basketball_1h_spread_locked"
