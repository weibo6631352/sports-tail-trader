"""未建模分段盘口（ML / spread）的精确拒绝原因回归测试。

冰球分节、棒球前 5 局（F5）等分段 ML/spread 当前没有干净的分段比分模型。
此前它们落到整场 sport 评估器，被拒成 missing_*_state（暗示数据缺失）；
现在改为精确的 unsupported_period_moneyline / unsupported_period_spread，
明确区分"分段类型未建模"与真实数据缺失。
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


def _hockey_game():
    return live_game_state_from_metadata({
        "league": "NHL",
        "sport": "ice-hockey",
        "home_name": "Boston Bruins",
        "away_name": "Toronto Maple Leafs",
        "home_score": 2,
        "away_score": 1,
        "period": "P2",
        "status": "live",
        "seconds_remaining": 300,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _baseball_game():
    return live_game_state_from_metadata({
        "league": "MLB",
        "sport": "baseball",
        "home_name": "New York Yankees",
        "away_name": "Boston Red Sox",
        "home_score": 3,
        "away_score": 2,
        "period": "T6",
        "status": "live",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "baseball_state": {
            "current_inning": 6,
            "inning_half": "top",
            "home_inning_runs": [1, 0, 1, 0, 1, None, None, None, None],
            "away_inning_runs": [0, 1, 0, 1, 0, None, None, None, None],
        },
    })


def _market(
    market_type: SportsMarketType,
    side: SportsMarketSide,
    line: str | None,
    slug: str,
) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=market_type,
        side=side,
        token_id="test",
        line=None if line is None else Decimal(line),
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug=slug,
    )


def test_hockey_period_moneyline_precise_reject() -> None:
    game = _hockey_game()
    assert game is not None
    m = _market(
        SportsMarketType.MONEYLINE,
        SportsMarketSide.HOME,
        None,
        "nhl-bos-tor-2026-05-22-1p-moneyline",
    )
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    # 精确原因，明确"分段未建模"——不能是误导性的 missing_*_state。
    assert ev.reason == TailRejectReason.UNSUPPORTED_PERIOD_MONEYLINE.value
    assert "missing" not in ev.reason


def test_hockey_period_spread_precise_reject() -> None:
    game = _hockey_game()
    assert game is not None
    m = _market(
        SportsMarketType.SPREADS,
        SportsMarketSide.HOME,
        "-1.5",
        "nhl-bos-tor-2026-05-22-first-period-spread-home-minus-1pt5",
    )
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_PERIOD_SPREAD.value
    assert "missing" not in ev.reason


def test_baseball_f5_moneyline_precise_reject() -> None:
    game = _baseball_game()
    assert game is not None
    m = _market(
        SportsMarketType.MONEYLINE,
        SportsMarketSide.HOME,
        None,
        "mlb-nyy-bos-2026-05-22-f5-moneyline",
    )
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_PERIOD_MONEYLINE.value
    assert ev.reason != TailRejectReason.MISSING_BASEBALL_STATE.value


def test_baseball_first_5_innings_spread_precise_reject() -> None:
    game = _baseball_game()
    assert game is not None
    m = _market(
        SportsMarketType.SPREADS,
        SportsMarketSide.AWAY,
        "+1.5",
        "mlb-nyy-bos-2026-05-22-first-5-innings-spread-away-plus-1pt5",
    )
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_PERIOD_SPREAD.value
    assert ev.reason != TailRejectReason.MISSING_BASEBALL_STATE.value
