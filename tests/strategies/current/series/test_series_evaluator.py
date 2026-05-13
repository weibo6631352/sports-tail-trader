"""Series evaluator 在 worktree 2 阶段的 record-only 行为。"""

from __future__ import annotations

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from strategies.current.series import (
    SeriesCandidate,
    SeriesRejectReason,
    SeriesSubType,
    evaluate_series_opportunity,
)


def _candidate(question: str, outcomes: tuple[tuple[str, str], ...] = (("tok-a", "Team A"),)) -> SeriesCandidate:
    market = Market(
        condition_id="cond-series",
        market_slug="test-slug",
        market_question=question,
        event_title=question,
        category="Sports",
        tags=("NBA",),
        outcomes=tuple(MarketOutcome(token_id=tid, outcome=label) for tid, label in outcomes),
        trading_status=TradingStatus.ELIGIBLE,
    )
    return SeriesCandidate(
        market=market,
        outcome_label=outcomes[0][1],
        token_id=outcomes[0][0],
    )


def test_winner_sub_type_without_state_returns_missing_series_state() -> None:
    # 不注入 inputs / state → record-only：classifier 判到 WINNER 但缺 state
    # 后立即 MISSING_SERIES_STATE。
    evaluation = evaluate_series_opportunity(_candidate("Who will win the series?"))

    assert evaluation.accepted is False
    assert evaluation.sub_type == SeriesSubType.WINNER
    assert evaluation.reject_reason == SeriesRejectReason.MISSING_SERIES_STATE
    assert evaluation.metadata["sub_type"] == "winner"
    assert evaluation.metadata["reject_reason"] == "missing_series_state"


def test_total_games_sub_type_without_state_returns_missing_state() -> None:
    # TOTAL_GAMES 子类型：先解析 outcome 再读 state；缺 state 报 MISSING_SERIES_STATE。
    evaluation = evaluate_series_opportunity(_candidate("Total Games O/U 5.5", outcomes=(("tok-over", "Over 5.5"),)))

    assert evaluation.accepted is False
    assert evaluation.sub_type == SeriesSubType.TOTAL_GAMES
    assert evaluation.reject_reason == SeriesRejectReason.MISSING_SERIES_STATE


def test_total_games_sub_type_with_unparseable_outcome_returns_outcome_not_parsed() -> None:
    # 解析失败优先于 state 缺失检查——outcome 写错时直接报 SERIES_OUTCOME_NOT_PARSED。
    evaluation = evaluate_series_opportunity(_candidate("Total Games scenario", outcomes=(("tok-unknown", "Maybe"),)))

    assert evaluation.accepted is False
    assert evaluation.sub_type == SeriesSubType.TOTAL_GAMES
    assert evaluation.reject_reason == SeriesRejectReason.SERIES_OUTCOME_NOT_PARSED


def test_game_handicap_sub_type_without_state_returns_missing_state() -> None:
    evaluation = evaluate_series_opportunity(
        _candidate("Game 5 handicap -3.5", outcomes=(("tok-yes", "Yes"),))
    )

    assert evaluation.accepted is False
    assert evaluation.sub_type == SeriesSubType.GAME_HANDICAP
    assert evaluation.reject_reason == SeriesRejectReason.MISSING_SERIES_STATE


def test_other_sub_type_returns_subtype_unclassified() -> None:
    # 没有任何系列赛关键词——classifier 落 OTHER，evaluator 返回 SUBTYPE_UNCLASSIFIED。
    # 该路径在生产里只在上游 family 归属误判时触达，作为 audit 入口。
    evaluation = evaluate_series_opportunity(_candidate("Lakers vs Knicks moneyline"))

    assert evaluation.accepted is False
    assert evaluation.sub_type == SeriesSubType.OTHER
    assert evaluation.reject_reason == SeriesRejectReason.SUBTYPE_UNCLASSIFIED
