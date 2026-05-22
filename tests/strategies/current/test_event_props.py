"""利基事件型 prop 盘口评估回归测试。

覆盖 7 类此前落到泛化 binary_prop 分支的盘口：
  - 建模类（clean-sheet / race-to-N / draw-no-bet / double-chance）：
    各验证 locked YES / locked NO / not-locked 三态。
  - 精确拒绝类（odd/even total / winning-margin / to-score-first）：
    验证返回 distinct 拒绝原因，而非泛化的 OUTCOME_NOT_LOCKED /
    binary_prop_no_tail_model。
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


def _game(home_score: int, away_score: int, status: str = "live"):
    return live_game_state_from_metadata({
        "league": "Premier League",
        "sport": "soccer",
        "home_name": "Arsenal",
        "away_name": "Chelsea",
        "home_score": home_score,
        "away_score": away_score,
        "period": "second_half",
        "status": status,
        "seconds_remaining": 300,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _market(
    side: SportsMarketSide,
    slug: str,
    *,
    sports_market_type: str | None = None,
) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug=slug,
        sports_market_type=sports_market_type,
    )


# ---- clean-sheet（建模） ---------------------------------------------


def test_clean_sheet_no_locked_when_opponent_scored() -> None:
    # 针对主队零封；客队已进 1 球 → 零封不可能 → NO 锁定（盘中即可）。
    game = _game(home_score=2, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.NO, "epl1-ars-che-2026-05-22-clean-sheet-home"),
        policy=TailPolicy(),
    )
    assert ev.accepted, ev.reason
    assert ev.reason == "clean_sheet_no_locked"


def test_clean_sheet_yes_locked_when_game_ended_opponent_zero() -> None:
    # 针对主队零封；比赛结束、客队 0 分 → 零封成立 → YES 锁定。
    game = _game(home_score=1, away_score=0, status="ended")
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-clean-sheet-home"),
        policy=TailPolicy(),
    )
    assert ev.accepted, ev.reason
    assert ev.reason == "clean_sheet_yes_locked"


def test_clean_sheet_yes_not_locked_live_opponent_zero() -> None:
    # 盘中客队仍 0 分——比赛未结束，客队随时可能进球 → YES 不锁定。
    game = _game(home_score=1, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-clean-sheet-home"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


# ---- race-to-N（建模） -----------------------------------------------


def _basketball_game(home_score: int, away_score: int):
    return live_game_state_from_metadata({
        "league": "NBA",
        "sport": "basketball",
        "home_name": "Lakers",
        "away_name": "Celtics",
        "home_score": home_score,
        "away_score": away_score,
        "period": "Q4",
        "status": "live",
        "seconds_remaining": 120,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def test_race_to_n_yes_locked_when_team_reached_first() -> None:
    # race-to-20 针对主队；主队 21、客队 18 → 主队先到 20 → YES 锁定。
    game = _basketball_game(home_score=21, away_score=18)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "nba1-lal-bos-2026-05-22-race-to-20-home"),
        policy=TailPolicy(),
    )
    assert ev.accepted, ev.reason
    assert ev.reason == "race_to_n_yes_locked"


def test_race_to_n_no_locked_when_opponent_reached_first() -> None:
    # race-to-20 针对主队；客队 22、主队 17 → 客队先到 → NO 锁定。
    game = _basketball_game(home_score=17, away_score=22)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.NO, "nba1-lal-bos-2026-05-22-race-to-20-home"),
        policy=TailPolicy(),
    )
    assert ev.accepted, ev.reason
    assert ev.reason == "race_to_n_no_locked"


def test_race_to_n_not_locked_when_neither_reached() -> None:
    # 双方均未到 20 → 先后顺序未定 → 不锁定。
    game = _basketball_game(home_score=18, away_score=19)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "nba1-lal-bos-2026-05-22-race-to-20-home"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


# ---- draw-no-bet（建模） ---------------------------------------------


def test_draw_no_bet_yes_locked_when_team_won() -> None:
    # 平局退款针对主队；终场主队 2-1 获胜 → YES 锁定。
    game = _game(home_score=2, away_score=1, status="ended")
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-draw-no-bet-home"),
        policy=TailPolicy(),
    )
    assert ev.accepted, ev.reason
    assert ev.reason == "draw_no_bet_yes_locked"


def test_draw_no_bet_no_locked_when_team_lost() -> None:
    # 平局退款针对主队；终场主队 0-2 落败 → 对手赢 → NO 锁定。
    game = _game(home_score=0, away_score=2, status="ended")
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.NO, "epl1-ars-che-2026-05-22-draw-no-bet-home"),
        policy=TailPolicy(),
    )
    assert ev.accepted, ev.reason
    assert ev.reason == "draw_no_bet_no_locked"


def test_draw_no_bet_not_locked_live() -> None:
    # 盘中领先——平局退款使"领先即锁"不成立 → 不锁定。
    game = _game(home_score=2, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-draw-no-bet-home"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


# ---- double-chance（建模） -------------------------------------------


def test_double_chance_yes_locked_when_outcome_covered() -> None:
    # 双重机会 1X（主胜或平）；终场平局 1-1 → 结果在覆盖集 → YES 锁定。
    game = _game(home_score=1, away_score=1, status="ended")
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-double-chance-1x"),
        policy=TailPolicy(),
    )
    assert ev.accepted, ev.reason
    assert ev.reason == "double_chance_yes_locked"


def test_double_chance_no_locked_when_excluded_outcome() -> None:
    # 双重机会 1X（主胜或平）；终场客队 0-2 获胜 → 落在排除结果 → NO 锁定。
    game = _game(home_score=0, away_score=2, status="ended")
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.NO, "epl1-ars-che-2026-05-22-double-chance-1x"),
        policy=TailPolicy(),
    )
    assert ev.accepted, ev.reason
    assert ev.reason == "double_chance_no_locked"


def test_double_chance_not_locked_live() -> None:
    # 盘中比分仍可变 → 不锁定。
    game = _game(home_score=1, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-double-chance-1x"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.OUTCOME_NOT_LOCKED.value


# ---- 精确拒绝类 ------------------------------------------------------


def test_odd_even_total_precise_reject() -> None:
    game = _game(home_score=1, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-total-goals-odd-even"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_ODD_EVEN.value
    assert ev.reason != TailRejectReason.OUTCOME_NOT_LOCKED.value
    assert ev.reason != "binary_prop_no_tail_model"


def test_winning_margin_precise_reject() -> None:
    game = _game(home_score=2, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-winning-margin-2"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_WINNING_MARGIN.value
    assert ev.reason != "binary_prop_no_tail_model"


def test_to_score_first_precise_reject() -> None:
    game = _game(home_score=1, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-first-goal-home"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_TO_SCORE_FIRST.value
    assert ev.reason != "binary_prop_no_tail_model"


def test_odd_even_on_mlb_game_keeps_precise_reject() -> None:
    # 利基事件 prop 跨运动通用——MLB 比赛上的总分奇偶盘口不能被 MLB 专属分支
    # 的 UNSUPPORTED_MARKET_TYPE 兜底吞掉，仍须给精确原因。
    game = live_game_state_from_metadata({
        "league": "USA: MLB",
        "sport": "baseball",
        "home_name": "Yankees",
        "away_name": "Red Sox",
        "home_score": 3,
        "away_score": 2,
        "period": "8",
        "status": "live",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "mlb1-nyy-bos-2026-05-22-total-runs-odd-even"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_ODD_EVEN.value


def test_to_score_first_precise_reject_ended_game() -> None:
    # 即使比赛已结束，缺首得分方数据仍精确拒绝（不能用终场比分臆测首球方）。
    game = _game(home_score=2, away_score=1, status="ended")
    assert game is not None
    ev = evaluate_tail_opportunity(
        game, _market(SportsMarketSide.YES, "epl1-ars-che-2026-05-22-to-score-first-home"),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_TO_SCORE_FIRST.value


# ---- sportsMarketType 首选识别（slug 关键字漏识别时的兜底） -----------


def test_odd_even_recognized_via_sports_market_type_when_slug_misses() -> None:
    # slug 不含任何 odd/even 关键字，旧的纯 slug 逻辑会漏掉 → 落到泛化拒绝；
    # Gamma sportsMarketType=basketball_odd_even 仍能精确识别。
    game = _game(home_score=1, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "nba-okc-sas-2026-05-22-parity",
            sports_market_type="basketball_odd_even",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_ODD_EVEN.value
    assert ev.reason != "binary_prop_no_tail_model"


def test_odd_even_recognized_via_slug_when_sports_market_type_null() -> None:
    # sportsMarketType 为空时仍走 slug 关键字回退。
    game = _game(home_score=1, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "epl1-ars-che-2026-05-22-total-goals-odd-even",
            sports_market_type=None,
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_ODD_EVEN.value


def test_to_score_first_recognized_via_sports_market_type_when_slug_misses() -> None:
    # slug 无首得分方关键字 → 旧 slug 逻辑漏识别；sportsMarketType 仍精确识别。
    game = _game(home_score=1, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "nba-okc-sas-2026-05-22-opening-bucket",
            sports_market_type="basketball_team_to_score_first",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_TO_SCORE_FIRST.value
    assert ev.reason != "binary_prop_no_tail_model"


def test_to_score_first_recognized_via_slug_when_sports_market_type_null() -> None:
    # sportsMarketType 为空时仍走 slug 关键字回退。
    game = _game(home_score=1, away_score=0)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "epl1-ars-che-2026-05-22-first-goal-home",
            sports_market_type=None,
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason == TailRejectReason.UNSUPPORTED_TO_SCORE_FIRST.value


def test_non_matching_market_not_recognized_as_event_prop() -> None:
    # 普通胜负盘：slug 无关键字、sportsMarketType=moneyline → 不被识别为
    # 利基 prop，不应返回 odd/even 等精确拒绝原因。
    game = _game(home_score=1, away_score=1)
    assert game is not None
    ev = evaluate_tail_opportunity(
        game,
        _market(
            SportsMarketSide.YES,
            "epl1-ars-che-2026-05-22-some-binary-prop",
            sports_market_type="moneyline",
        ),
        policy=TailPolicy(),
    )
    assert not ev.accepted
    assert ev.reason not in {
        TailRejectReason.UNSUPPORTED_ODD_EVEN.value,
        TailRejectReason.UNSUPPORTED_TO_SCORE_FIRST.value,
        TailRejectReason.UNSUPPORTED_WINNING_MARGIN.value,
    }
