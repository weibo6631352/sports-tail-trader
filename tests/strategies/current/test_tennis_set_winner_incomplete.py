"""网球 set-winner 评估回归测试。

策略哲学是概率博弈，不要求 100% 锁定：当前盘强局分领先（5-x，净胜 ≥2）即
可作为概率性提前入场。但 set_scores 里进行中的局分不能被当成"该盘已完成
胜出"——Path B（数学锁定）必须确认该盘真的打完。
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


def _game(tennis_state: dict):
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
        "tennis_state": tennis_state,
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


def test_set_winner_accepted_on_strong_lead_in_progress() -> None:
    # 首盘进行中、Maristany 5-2 领先（current_set=1）→ 概率性提前入场接受。
    game = _game({
        "current_set": 1,
        "set_scores": [[5, 2]],
        "home_current_set_games": 5,
        "away_current_set_games": 2,
        "home_sets_won": 0,
        "away_sets_won": 0,
    })
    assert game is not None
    ev = evaluate_tail_opportunity(game, _first_set_winner_market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, f"5-2 强领先应作为概率性入场被接受；reason={ev.reason}"
    assert ev.reason == "tennis_set_winner_current_set_near_locked"


def test_set_winner_rejected_on_weak_lead() -> None:
    # 首盘 3-2，领先不够强、盘也没打完 → 拒绝。
    game = _game({
        "current_set": 1,
        "set_scores": [[3, 2]],
        "home_current_set_games": 3,
        "away_current_set_games": 2,
        "home_sets_won": 0,
        "away_sets_won": 0,
    })
    assert game is not None
    ev = evaluate_tail_opportunity(game, _first_set_winner_market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted


def test_set_winner_accepted_on_completed_set() -> None:
    # 首盘 6-2 已打完、Maristany 胜 → Path B 数学锁定。
    game = _game({
        "current_set": 2,
        "set_scores": [[6, 2]],
        "home_sets_won": 1,
        "away_sets_won": 0,
    })
    assert game is not None
    ev = evaluate_tail_opportunity(game, _first_set_winner_market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "tennis_set_winner_locked"


def test_set_winner_rejected_when_completed_set_lost() -> None:
    # 首盘 2-6 已打完、Maristany 输 → 不锁定。
    game = _game({
        "current_set": 2,
        "set_scores": [[2, 6]],
        "home_sets_won": 0,
        "away_sets_won": 1,
    })
    assert game is not None
    ev = evaluate_tail_opportunity(game, _first_set_winner_market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
