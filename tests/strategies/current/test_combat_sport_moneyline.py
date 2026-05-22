"""拳击 / MMA 胜负盘扫尾评估回归测试。

格斗无可靠盘中比分模型——本系统不建立回合评分模型（CLAUDE.md §17）。
- 进行中的格斗赛 → 精确可审计拒绝 ``mma_in_progress_no_model``。
- 已结束的格斗赛 → participant.score（胜者 1）经 ended-moneyline 锁定胜方。
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


def _game(*, sport: str, status: str, home_score: int, away_score: int):
    return live_game_state_from_metadata({
        "league": "UFC" if sport == "mma" else "WBC",
        "sport": sport,
        "home_name": "Fighter A",
        "away_name": "Fighter B",
        "home_score": home_score,
        "away_score": away_score,
        "period": "",
        "status": status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _market(side: SportsMarketSide) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("20"),
        market_slug="ufc-fighter-a-vs-fighter-b-2026-05-23",
    )


@pytest.mark.parametrize("sport", ["mma", "boxing"])
def test_combat_in_progress_precise_reject(sport: str) -> None:
    # 进行中的格斗赛——无盘中模型，给精确可审计原因（不是泛化的缺数据原因）。
    game = _game(sport=sport, status="live", home_score=0, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.MMA_IN_PROGRESS_NO_MODEL.value


@pytest.mark.parametrize("sport", ["mma", "boxing"])
def test_combat_finished_winner_accepted(sport: str) -> None:
    # 已结束的格斗赛 → 胜方（home_score=1）经 ended-moneyline 锁定。
    game = _game(sport=sport, status="ended", home_score=1, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "ended_not_closed_moneyline"


@pytest.mark.parametrize("sport", ["mma", "boxing"])
def test_combat_finished_loser_rejected(sport: str) -> None:
    # 已结束的格斗赛，盘口选的是输方 → 不锁定。
    game = _game(sport=sport, status="ended", home_score=1, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.AWAY), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value
