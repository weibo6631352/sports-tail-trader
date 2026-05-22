"""运动专属 prop 家族的精确拒绝原因回归测试。

覆盖三类此前落到泛化 binary_prop 分支的盘口：
  - 拳击/MMA 胜利方式（method-of-victory）
  - F1 子盘口（杆位/登台/最快圈速/安全车）
  - 板球 prop（掷币胜方/最佳击球手/最多六分球）

每类各验证：① sportsMarketType 命中 → distinct 精确拒绝原因；
② slug 关键字回退（sportsMarketType 缺失）→ 同样精确原因；
③ 拒绝原因绝不是泛化的 binary_prop_no_tail_model / OUTCOME_NOT_LOCKED。
另验证 F1 整场冠军（race winner）不被误归 F1-prop 拒绝，以及 esports prop
家族级拒绝原因不受影响。
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


def _mma_game(status: str = "live"):
    # MMA/拳击没有专属 GameState；用通用 LiveGameState（无比分语义）模拟。
    return live_game_state_from_metadata({
        "league": "UFC",
        "sport": "mma",
        "home_name": "Fighter A",
        "away_name": "Fighter B",
        "home_score": 0,
        "away_score": 0,
        "status": status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _f1_game(status: str = "live"):
    return live_game_state_from_metadata({
        "league": "Formula 1",
        "sport": "amfootball",
        "home_name": "Driver A",
        "away_name": "Driver B",
        "home_score": 0,
        "away_score": 0,
        "status": status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _cricket_game(status: str = "live"):
    return live_game_state_from_metadata({
        "league": "IPL",
        "sport": "cricket",
        "home_name": "Mumbai",
        "away_name": "Chennai",
        "home_score": 0,
        "away_score": 0,
        "status": status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _market(
    side: SportsMarketSide,
    slug: str,
    *,
    market_type: SportsMarketType = SportsMarketType.BINARY_PROP,
    sports_market_type: str | None = None,
) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=market_type,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.50"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug=slug,
        sports_market_type=sports_market_type,
    )


# ---- 拳击/MMA 胜利方式 -----------------------------------------------


def test_method_of_victory_recognized_by_sports_market_type() -> None:
    # sportsMarketType=ufc_method_of_victory 是首选识别信号（Gamma 可靠填充）。
    game = _mma_game()
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.HOME,
            "ufc-fight-a-vs-b-2026-05-22-result",
            market_type=SportsMarketType.MONEYLINE,
            sports_market_type="ufc_method_of_victory",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_METHOD_OF_VICTORY.value
    assert ev.reason != TailRejectReason.OUTCOME_NOT_LOCKED.value
    assert ev.reason != "binary_prop_no_tail_model"


def test_method_of_victory_slug_keyword_fallback() -> None:
    # sportsMarketType 缺失时回退 slug 关键字（win-by-ko-tko）。
    game = _mma_game()
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(SportsMarketSide.YES, "ufc-fight-a-vs-b-2026-05-22-win-by-ko-tko"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_METHOD_OF_VICTORY.value


def test_method_of_victory_go_the_distance_keyword() -> None:
    game = _mma_game()
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(SportsMarketSide.YES, "ufc-fight-a-vs-b-2026-05-22-go-the-distance"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_METHOD_OF_VICTORY.value


def test_method_of_victory_precise_reject_when_game_ended() -> None:
    # 比赛已结束仍精确拒绝——归一化直播模型不携带胜利方式遥测。
    game = _mma_game(status="ended")
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.HOME,
            "ufc-fight-a-vs-b-2026-05-22-result",
            market_type=SportsMarketType.MONEYLINE,
            sports_market_type="ufc_method_of_victory",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_METHOD_OF_VICTORY.value


# ---- F1 子盘口 -------------------------------------------------------


def test_f1_qualifying_pole_recognized_by_sports_market_type() -> None:
    game = _f1_game()
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "monaco-gp-2026-pole",
            sports_market_type="f1_qualifying_pole",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_F1_PROP.value
    assert ev.reason != "binary_prop_no_tail_model"


def test_f1_race_podium_recognized_by_sports_market_type() -> None:
    game = _f1_game()
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "monaco-gp-2026-podium",
            sports_market_type="f1_race_podium",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_F1_PROP.value


def test_f1_prop_slug_keyword_fallback() -> None:
    # sportsMarketType 缺失时回退 slug 关键字（fastest-lap / safety-car）。
    game = _f1_game()
    assert game is not None
    for slug in (
        "monaco-gp-2026-fastest-lap",
        "monaco-gp-2026-safety-car",
        "monaco-gp-2026-pole-position",
    ):
        ev = evaluate_tail_opportunity(
            game, _market(SportsMarketSide.YES, slug), policy=TailPolicy()
        )
        assert not ev.accepted, slug
        assert ev.reason == TailRejectReason.UNSUPPORTED_F1_PROP.value, slug


def test_f1_race_winner_not_classified_as_f1_prop() -> None:
    # F1 整场冠军（race winner）不归 F1-prop 精确拒绝——由 RACE-kind 直播匹配
    # 链路另行处理；此处只验证它不会拿到 UNSUPPORTED_F1_PROP。
    game = _f1_game()
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "monaco-gp-2026-f1-race-winner",
            sports_market_type="f1_race_winner",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason != TailRejectReason.UNSUPPORTED_F1_PROP.value


# ---- 板球 prop -------------------------------------------------------


def test_cricket_toss_winner_recognized_by_sports_market_type() -> None:
    game = _cricket_game()
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "ipl-mum-che-2026-05-22-toss",
            sports_market_type="cricket_toss_winner",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_CRICKET_PROP.value
    assert ev.reason != "binary_prop_no_tail_model"


def test_cricket_top_batter_recognized_by_sports_market_type() -> None:
    game = _cricket_game()
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "ipl-mum-che-2026-05-22-top-batter",
            sports_market_type="cricket_team_top_batter",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_CRICKET_PROP.value


def test_cricket_prop_slug_keyword_fallback() -> None:
    # sportsMarketType 缺失时回退 slug 关键字（most-sixes / top-batter）。
    game = _cricket_game()
    assert game is not None
    for slug in (
        "ipl-mum-che-2026-05-22-most-sixes",
        "ipl-mum-che-2026-05-22-top-batter",
        "ipl-mum-che-2026-05-22-toss-winner",
    ):
        ev = evaluate_tail_opportunity(
            game, _market(SportsMarketSide.YES, slug), policy=TailPolicy()
        )
        assert not ev.accepted, slug
        assert ev.reason == TailRejectReason.UNSUPPORTED_CRICKET_PROP.value, slug


# ---- esports prop 家族级拒绝不受影响 ---------------------------------


def test_esports_prop_family_reject_unchanged() -> None:
    # esports prop（odd/even kills、rampage 等）在 family 级即被拒为
    # esports_market_not_auto_tradable——本次改动不应改变该可审计原因。
    game = live_game_state_from_metadata({
        "league": "CS2",
        "sport": "esports",
        "home_name": "Team A",
        "away_name": "Team B",
        "home_score": 0,
        "away_score": 0,
        "status": "live",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })
    assert game is not None
    from strategies.current.tail import SportsMarketFamily

    snapshot = SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=SportsMarketSide.YES,
        token_id="test",
        line=None,
        best_ask=Decimal("0.50"),
        buyable_liquidity_usdc=Decimal("10"),
        market_family=SportsMarketFamily.ESPORTS,
        market_slug="cs2-team-a-vs-b-2026-05-22-odd-even-total-kills",
        sports_market_type="cs2_odd_even_total_kills",
    )
    ev = evaluate_tail_opportunity(game, snapshot, policy=TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ESPORTS_MARKET_NOT_AUTO_TRADABLE.value
