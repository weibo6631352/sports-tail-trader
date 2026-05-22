"""MLB Under 总分入场门禁（margin 随局数缩放）回归测试。

旧门禁要求"九局下两出局"才放行 Under 总分，过度保守、系统性放弃八局即已
锁定的正期望机会。新门禁让所需 safety margin 随局数缩放：margin 足够大时
更早的局即可入场，配合提前止盈做准量化盈利。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from strategies.current.tail import (
    SportsMarketSnapshot,
    SportsMarketSide,
    SportsMarketType,
    TailRejectReason,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)
from strategies.current.tail.types import TailPolicy


def _policy() -> TailPolicy:
    # 默认即"激进"档：9 局基准 margin=2，每提前一局 +2，最早 6 局。
    return TailPolicy(
        min_under_safety_margin=Decimal("2"),
        mlb_under_min_inning=6,
        mlb_under_inning_margin_step=Decimal("2"),
    )


def _mlb_game(inning: int, home_score: int, away_score: int):
    return live_game_state_from_metadata({
        "league": "MLB",
        "sport": "baseball",
        "home_name": "Arizona Diamondbacks",
        "away_name": "Colorado Rockies",
        "home_score": home_score,
        "away_score": away_score,
        "period": f"Inning {inning}",
        "status": "live",
        "seconds_remaining": 1800,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "baseball_state": {
            "current_inning": inning,
            "inning_half": "top",
            "outs": 1,
            "occupied_bases": [],
        },
    })


def _under_market(line: str) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.TOTALS,
        side=SportsMarketSide.UNDER,
        token_id="test",
        line=Decimal(line),
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
    )


@pytest.mark.parametrize(
    ("inning", "total", "line", "should_accept"),
    [
        # 9 局：基准 margin 2
        (9, 4, "6.5", True),    # margin 2.5 >= 2
        (9, 8, "9.5", False),   # margin 1.5 < 2
        # 8 局：需 margin 4
        (8, 2, "6.5", True),    # margin 4.5 >= 4
        (8, 4, "6.5", False),   # margin 2.5 < 4
        # 7 局：需 margin 6
        (7, 2, "8.5", True),    # margin 6.5 >= 6
        (7, 2, "7.5", False),   # margin 5.5 < 6
        # 6 局：需 margin 8
        (6, 1, "9.5", True),    # margin 8.5 >= 8
        (6, 1, "8.5", False),   # margin 7.5 < 8
    ],
)
def test_mlb_under_totals_margin_scaled_by_inning(
    inning: int, total: int, line: str, should_accept: bool
) -> None:
    game = _mlb_game(inning, total, 0)
    assert game is not None
    evaluation = evaluate_tail_opportunity(game, _under_market(line), policy=_policy())
    assert evaluation.accepted is should_accept, f"reason={evaluation.reason}"


def test_mlb_under_totals_rejected_before_min_inning() -> None:
    # 5 局即使 margin 极大也拒绝：早于 mlb_under_min_inning。
    game = _mlb_game(5, 0, 0)
    assert game is not None
    evaluation = evaluate_tail_opportunity(game, _under_market("12.5"), policy=_policy())
    assert not evaluation.accepted
    assert evaluation.reason == TailRejectReason.BASEBALL_NOT_LATE_ENOUGH.value
