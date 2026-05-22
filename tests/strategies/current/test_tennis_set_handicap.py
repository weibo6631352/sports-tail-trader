"""网球盘分让分（set handicap）评估回归测试。

盘分让分按整场盘数差结算（-1.5 / +1.5，单位为盘）。此前网球 SPREADS 一律
被 TENNIS_SPREADS_NOT_SUPPORTED 硬拒；盘分让分可由 TennisGameState 的盘数 +
best-of 数学锁定。
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


def _game(*, best_of: int | None, home_sets_won: int, away_sets_won: int):
    return live_game_state_from_metadata({
        "league": "ATP Rome",
        "sport": "tennis",
        "home_name": "C. Alcaraz",
        "away_name": "J. Sinner",
        "home_score": 0,
        "away_score": 0,
        "period": "Set 2",
        "status": "live",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "tennis_state": {
            "best_of": best_of,
            "home_sets_won": home_sets_won,
            "away_sets_won": away_sets_won,
            "current_set": home_sets_won + away_sets_won + 1,
        },
    })


def _handicap_market(
    side: SportsMarketSide,
    line: str,
    *,
    slug: str | None = None,
    sports_market_type: str | None = None,
) -> SportsMarketSnapshot:
    sign = "minus" if line.startswith("-") else "plus"
    digits = line.lstrip("+-").replace(".", "pt")
    default_slug = f"atp-rome-alcaraz-sinner-2026-05-22-set-handicap-{sign}-{digits}"
    return SportsMarketSnapshot(
        market_type=SportsMarketType.SPREADS,
        side=side,
        token_id="test",
        line=Decimal(line),
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("20"),
        market_slug=slug if slug is not None else default_slug,
        sports_market_type=sports_market_type,
    )


# ---- best-of-3 -1.5 ---------------------------------------------------


def test_minus_1pt5_locked_yes_at_2_0() -> None:
    # best-of-3，home 2-0 取胜 → 盘差 2，2-1.5>0 → 让分锁定 YES。
    game = _game(best_of=3, home_sets_won=2, away_sets_won=0)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "-1.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "tennis_set_handicap_locked_yes"


def test_minus_1pt5_locked_no_once_opponent_wins_a_set() -> None:
    # 对手赢下 1 盘后，home 最多 2-1（盘差 1）< 1.5 → -1.5 不可能覆盖。
    game = _game(best_of=3, home_sets_won=1, away_sets_won=1)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "-1.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    # 数学锁定为 NO → 落到赔率差价拒绝原因（SPREADS 第二入场路径）。
    assert ev.reason in {
        TailRejectReason.NO_ODDS_GAP.value,
        TailRejectReason.ODDS_GAP_LINE_MISMATCH.value,
    }


def test_minus_1pt5_locked_no_even_when_opponent_only_leads() -> None:
    game = _game(best_of=3, home_sets_won=0, away_sets_won=1)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "-1.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted


# ---- best-of-3 +1.5 ---------------------------------------------------


def test_plus_1pt5_locked_yes_once_side_wins_a_set() -> None:
    # home 赢下 1 盘 → 最差 1-2（盘差 -1），-1+1.5>0 → +1.5 锁定 YES。
    game = _game(best_of=3, home_sets_won=1, away_sets_won=0)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "+1.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "tennis_set_handicap_locked_yes"


def test_plus_1pt5_not_locked_before_any_set() -> None:
    # 0-0：home 仍可能 0-2 输掉，+1.5 未锁定。
    game = _game(best_of=3, home_sets_won=0, away_sets_won=0)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "+1.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason in {
        TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value,
        TailRejectReason.NO_ODDS_GAP.value,
    }


# ---- best-of-5 --------------------------------------------------------


def test_best_of_5_minus_1pt5_locked_yes_at_3_1() -> None:
    # best-of-5，home 3-1 取胜 → 盘差 2，2-1.5>0 → 锁定 YES。
    game = _game(best_of=5, home_sets_won=3, away_sets_won=1)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "-1.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "tennis_set_handicap_locked_yes"


def test_best_of_5_minus_1pt5_not_locked_at_2_2() -> None:
    # 2-2：home 最好 3-2（盘差 1）< 1.5 → 数学锁定 NO。
    game = _game(best_of=5, home_sets_won=2, away_sets_won=2)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "-1.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted


def test_best_of_5_plus_1pt5_locked_yes_once_two_sets_won() -> None:
    # home 已赢 2 盘 → 最差 2-3（盘差 -1），-1+1.5>0 → +1.5 锁定 YES。
    game = _game(best_of=5, home_sets_won=2, away_sets_won=1)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "+1.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "tennis_set_handicap_locked_yes"


# ---- best-of 未知 / 不支持线 -----------------------------------------


def test_best_of_unknown_precise_reject() -> None:
    # best-of 缺失 → 无法确定取胜所需盘数 → 精确拒绝，非泛化原因。
    game = _game(best_of=None, home_sets_won=2, away_sets_won=0)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "-1.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.TENNIS_BEST_OF_UNKNOWN.value


def test_unsupported_line_precise_reject() -> None:
    # best-of-3 的 -2.5 让分不可能成立（最大盘差 2）→ 精确标记不支持线。
    game = _game(best_of=3, home_sets_won=1, away_sets_won=0)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "-2.5")
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.TENNIS_SET_HANDICAP_LINE_UNSUPPORTED.value


# ---- sportsMarketType 首选识别 ---------------------------------------


def test_set_handicap_recognized_via_sports_market_type_when_slug_misses() -> None:
    # slug 无 "set handicap" 关键字，旧的纯 slug 逻辑会漏识别 → 落到网球
    # SPREADS 通用拒绝；Gamma sportsMarketType=tennis_set_handicap 仍能精确
    # 识别并走盘分让分锁定评估（best-of-3 home 2-0 → 锁定 YES）。
    game = _game(best_of=3, home_sets_won=2, away_sets_won=0)
    assert game is not None
    m = _handicap_market(
        SportsMarketSide.HOME,
        "-1.5",
        slug="atp-rome-alcaraz-sinner-2026-05-22-sh-away-1pt5",
        sports_market_type="tennis_set_handicap",
    )
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "tennis_set_handicap_locked_yes"


def test_set_handicap_recognized_via_slug_when_sports_market_type_null() -> None:
    # sportsMarketType 为空时仍走 slug 关键字回退（"set handicap"）。
    game = _game(best_of=3, home_sets_won=2, away_sets_won=0)
    assert game is not None
    m = _handicap_market(SportsMarketSide.HOME, "-1.5", sports_market_type=None)
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.reason == "tennis_set_handicap_locked_yes"


def test_non_handicap_tennis_spread_still_rejected() -> None:
    # 网球局数让分（games handicap）仍无模型 → 保留 TENNIS_SPREADS_NOT_SUPPORTED。
    game = _game(best_of=3, home_sets_won=1, away_sets_won=0)
    assert game is not None
    m = SportsMarketSnapshot(
        market_type=SportsMarketType.SPREADS,
        side=SportsMarketSide.HOME,
        token_id="test",
        line=Decimal("-3.5"),
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("20"),
        market_slug="atp-rome-alcaraz-sinner-2026-05-22-games-handicap-minus-3pt5",
    )
    ev = evaluate_tail_opportunity(game, m, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason in {
        TailRejectReason.TENNIS_SPREADS_NOT_SUPPORTED.value,
        TailRejectReason.NO_ODDS_GAP.value,
        TailRejectReason.ODDS_GAP_LINE_MISMATCH.value,
    }
