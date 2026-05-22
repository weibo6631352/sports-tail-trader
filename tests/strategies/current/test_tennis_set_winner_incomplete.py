"""网球 set-winner 盘口未完成盘回归测试。

历史 bug（实盘亏损）：set_scores 里可能是进行中的局分（如 5-2），
_evaluate_tennis_set_winner 把它当成"该盘已完成、领先方胜"→ 在未决出的盘上
误判锁定并下单。current_set 错位（标 2 实为 1）会让评估走到这条分支。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from strategies.current.tail import (
    SportsMarketSnapshot,
    SportsMarketSide,
    SportsMarketType,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)
from strategies.current.tail.types import TailPolicy


def _game(set_scores: list, current_set: int):
    return live_game_state_from_metadata({
        "league": "WTA French Open Qualification",
        "sport": "tennis",
        "home_name": "G. Maristany",
        "away_name": "K. Quevedo",
        "home_score": 0,
        "away_score": 0,
        "period": "Set 1",
        "status": "live",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "tennis_state": {
            "home_sets_won": 0,
            "away_sets_won": 0,
            "current_set": current_set,
            "set_scores": set_scores,
        },
    })


def _first_set_winner_market(side: SportsMarketSide) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.88"),
        buyable_liquidity_usdc=Decimal("20"),
        market_slug="wta-marista-quevedo-2026-05-22-first-set-winner-Maristany-vs-Quevedo",
    )


def test_set_winner_rejected_on_incomplete_set() -> None:
    # set_scores 首盘 5-2（未打完），current_set 错位为 2 → 必须拒绝，不能当锁定。
    game = _game(set_scores=[[5, 2]], current_set=2)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _first_set_winner_market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted, f"未完成的盘不能判定 set winner 锁定；reason={ev.reason}"


def test_set_winner_accepted_on_completed_set() -> None:
    # 首盘 6-2 已打完、Maristany 胜 → first-set-winner Maristany 锁定。
    game = _game(set_scores=[[6, 2]], current_set=2)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _first_set_winner_market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "tennis_set_winner_locked"


def test_set_winner_rejected_when_completed_set_lost() -> None:
    # 首盘 2-6 已打完、Maristany 输 → first-set-winner Maristany 不锁定。
    game = _game(set_scores=[[2, 6]], current_set=2)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _first_set_winner_market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
