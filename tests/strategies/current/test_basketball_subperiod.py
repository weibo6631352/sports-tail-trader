"""篮球分段盘口（单节 Q1-Q4 / 下半场 2H）ML+spread 评估回归测试。

此前篮球单节 / 下半场 ML+spread 落到整场评估器，被误判为
missing_*_state（暗示数据缺失），实际是分段类型未建模。新增评估器从
BasketballGameState 的分节得分干净锁定，与 1H 评估同源。
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
        "seconds_remaining": 300,
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


# ---- 单节 Q1 ML / spread ---------------------------------------------


def test_q1_moneyline_locked_after_q1_ended() -> None:
    # Q1 30-25 home 领先；进入 Q2 → Q1 已结束 → home Q1 ML 锁定。
    game = _game(current_period=2, home_q=[30, None, None, None], away_q=[25, None, None, None])
    assert game is not None
    m = _market(SportsMarketType.MONEYLINE, SportsMarketSide.HOME, None, "1q-moneyline")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "basketball_q1_moneyline_locked"


def test_q1_moneyline_not_locked_mid_q1() -> None:
    # 仍在 Q1 → 该节未结束，不能锁定。
    game = _game(current_period=1, home_q=[18, None, None, None], away_q=[15, None, None, None])
    assert game is not None
    m = _market(SportsMarketType.MONEYLINE, SportsMarketSide.HOME, None, "1q-moneyline")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.BASKETBALL_QUARTER_NOT_COMPLETE.value


def test_q1_spread_covered_after_q1_ended() -> None:
    # Q1 margin home +5；spread home -2.5 → 5 + (-2.5) = 2.5 > 0 → 锁定覆盖。
    game = _game(current_period=2, home_q=[30, None, None, None], away_q=[25, None, None, None])
    assert game is not None
    m = _market(SportsMarketType.SPREADS, SportsMarketSide.HOME, "-2.5", "1q-spread-home-minus-2pt5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "basketball_q1_spread_locked"


def test_q3_moneyline_locked_after_q3_ended() -> None:
    # Q3 单节 22-20 home 领先；进入 Q4 → Q3 已结束 → home Q3 ML 锁定。
    game = _game(current_period=4, home_q=[28, 26, 22, None], away_q=[25, 24, 20, None])
    assert game is not None
    m = _market(SportsMarketType.MONEYLINE, SportsMarketSide.HOME, None, "3q-moneyline")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "basketball_q3_moneyline_locked"


# ---- 下半场 2H ML / spread -------------------------------------------


def test_2h_moneyline_locked_in_overtime() -> None:
    # 进入加时（period 5）→ 正赛 4 节打完 → 2H = Q3+Q4 已锁定。
    # 2H：home 22+24=46 vs away 20+19=39 → home 2H ML 锁定。
    game = _game(current_period=5, home_q=[28, 26, 22, 24], away_q=[25, 24, 20, 19])
    assert game is not None
    m = _market(SportsMarketType.MONEYLINE, SportsMarketSide.HOME, None, "2h-moneyline")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "basketball_2h_moneyline_locked"


def test_2h_not_locked_during_q4() -> None:
    # 仍在 Q4 → 下半场未结束。
    game = _game(current_period=4, home_q=[28, 26, 22, None], away_q=[25, 24, 20, None])
    assert game is not None
    m = _market(SportsMarketType.MONEYLINE, SportsMarketSide.HOME, None, "2h-moneyline")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.BASKETBALL_SECOND_HALF_NOT_COMPLETE.value


def test_2h_spread_covered_in_overtime() -> None:
    # 2H margin home +7；spread home -3.5 → 7 + (-3.5) = 3.5 > 0 → 锁定覆盖。
    game = _game(current_period=5, home_q=[28, 26, 22, 24], away_q=[25, 24, 20, 19])
    assert game is not None
    m = _market(SportsMarketType.SPREADS, SportsMarketSide.HOME, "-3.5", "2h-spread-home-minus-3pt5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "basketball_2h_spread_locked"
