"""手球（handball）胜负盘扫尾评估回归测试。

手球 livescore 无盘中时钟——锁定只能依赖超大领先（下半场 12 球 / 半场未知
16 球）或比赛已结束。其余给精确可审计拒绝原因。
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


def _game(
    *,
    status: str = "live",
    home_score: int,
    away_score: int,
    period: str | None = "second_half",
):
    state = {"period": period, "clock_minutes": None}
    return live_game_state_from_metadata({
        "league": "EHF Champions League",
        "sport": "handball",
        "home_name": "Kiel",
        "away_name": "Barcelona",
        "home_score": home_score,
        "away_score": away_score,
        "period": period or "",
        "status": status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "handball_state": state,
    })


def _market(side: SportsMarketSide) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("20"),
        market_slug="ehf-kiel-vs-barcelona-2026-05-23",
    )


def test_handball_metadata_round_trip_rebuilds_state() -> None:
    game = _game(home_score=40, away_score=25, period="second_half")
    assert game is not None
    assert game.handball_state is not None
    assert game.handball_state.period == "second_half"


def test_handball_second_half_large_lead_locked() -> None:
    # 下半场领先 12+ 球 → 锁定。
    game = _game(home_score=40, away_score=27, period="second_half")
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "handball_moneyline_large_lead"


def test_handball_second_half_lead_not_safe() -> None:
    # 下半场领先仅 8 球 → 未达 12 阈值，不锁定。
    game = _game(home_score=30, away_score=22, period="second_half")
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.HANDBALL_LEAD_NOT_SAFE.value


def test_handball_first_half_never_locked() -> None:
    # 上半场——还有完整下半场，任何盘中领先都不锁定。
    game = _game(home_score=20, away_score=5, period="first_half")
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.HANDBALL_LEAD_NOT_SAFE.value


def test_handball_no_period_requires_larger_lead() -> None:
    # period 未知：要求 16 球领先。14 球不够。
    game = _game(home_score=34, away_score=20, period=None)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.HANDBALL_LEAD_NOT_SAFE.value


def test_handball_no_period_large_lead_locked() -> None:
    # period 未知但领先 16+ → 锁定。
    game = _game(home_score=36, away_score=20, period=None)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "handball_moneyline_large_lead"


def test_handball_ended_winner_accepted() -> None:
    # 比赛结束 → 最终比分经 ended-moneyline 锁定胜方。
    game = _game(status="ended", home_score=30, away_score=28, period="second_half")
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "ended_not_closed_moneyline"
