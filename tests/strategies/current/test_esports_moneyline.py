"""电子竞技（esports）胜负盘评估回归测试。

esports 胜负盘是双方对阵 2-way MONEYLINE（哪支战队赢下 best-of-N 系列赛）。
锁定模型：某方 maps_won 达到 ``best_of // 2 + 1`` → 该方必胜。
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
    status: str,
    best_of: int | None,
    home_maps_won: int,
    away_maps_won: int,
):
    """esports LiveGameState：home_score/away_score = 各队已赢局数。"""
    return live_game_state_from_metadata({
        "league": "CS Asia Championships Group A",
        "sport": "esports",
        "home_name": "Team Falcons",
        "away_name": "Legacy",
        "home_score": home_maps_won,
        "away_score": away_maps_won,
        "period": "BO3",
        "status": status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "esports_state": {
            "best_of": best_of,
            "home_maps_won": home_maps_won,
            "away_maps_won": away_maps_won,
        },
    })


def _market(side: SportsMarketSide) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("20"),
        market_slug="cs-asia-falcons-vs-legacy-2026-05-22",
    )


def test_esports_metadata_round_trip_rebuilds_state() -> None:
    # live_game_state_from_metadata 必须重建 esports_state（否则评估器拿不到 best_of）。
    game = _game(status="live", best_of=3, home_maps_won=2, away_maps_won=0)
    assert game is not None
    assert game.esports_state is not None
    assert game.esports_state.best_of == 3
    assert game.esports_state.home_maps_won == 2


def test_esports_home_locked_when_maps_reach_needed() -> None:
    # BO3 需赢 2 局；主队 2-0 → "Falcons win" 锁定。
    game = _game(status="live", best_of=3, home_maps_won=2, away_maps_won=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "esports_moneyline_locked"


def test_esports_away_locked_in_bo5() -> None:
    # BO5 需赢 3 局；客队 3-1 → "Legacy win" 锁定。
    game = _game(status="live", best_of=5, home_maps_won=1, away_maps_won=3)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.AWAY), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "esports_moneyline_locked"


def test_esports_not_locked_when_maps_below_needed() -> None:
    # BO3 需赢 2 局；主队 1-1 → 未锁定。
    game = _game(status="live", best_of=3, home_maps_won=1, away_maps_won=1)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


def test_esports_losing_side_not_locked() -> None:
    # BO3：主队 2-0 已赢，但盘口选的是客队 → 客队未锁定。
    game = _game(status="live", best_of=3, home_maps_won=2, away_maps_won=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.AWAY), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


def test_esports_best_of_unknown_rejected() -> None:
    # best_of 无法解析（@round 为空）→ 可审计拒绝，不静默放行。
    game = _game(status="live", best_of=None, home_maps_won=2, away_maps_won=0)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ESPORTS_BEST_OF_UNKNOWN.value


def test_esports_ended_winner_accepted() -> None:
    # 比赛结束（Finished）→ maps 较多方判定为锁定胜者，走 ended-moneyline 评估器。
    game = _game(status="ended", best_of=3, home_maps_won=2, away_maps_won=1)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "ended_not_closed_moneyline"


def test_esports_ended_loser_rejected() -> None:
    game = _game(status="ended", best_of=3, home_maps_won=1, away_maps_won=2)
    assert game is not None
    ev = evaluate_tail_opportunity(game, _market(SportsMarketSide.HOME), policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value
