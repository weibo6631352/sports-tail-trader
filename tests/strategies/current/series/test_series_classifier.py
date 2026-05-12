"""Series 子类型分类器的行为契约。"""

from __future__ import annotations

import pytest

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from strategies.current.series import SeriesSubType, classify_series_sub_type


def _market(*, question: str, slug: str = "test-slug", event_title: str | None = None,
            outcomes: tuple[tuple[str, str], ...] = (("tok-a", "Team A"), ("tok-b", "Team B"))) -> Market:
    return Market(
        condition_id="cond-test",
        market_slug=slug,
        market_question=question,
        event_title=event_title or question,
        category="Sports",
        tags=("NBA",),
        outcomes=tuple(MarketOutcome(token_id=tid, outcome=label) for tid, label in outcomes),
        trading_status=TradingStatus.ELIGIBLE,
    )


@pytest.mark.parametrize(
    "question",
    [
        "Who will win the series?",
        "NBA Playoffs: Who Will Win Series? - Knicks vs. Hawks",
        "Series winner: Celtics or 76ers",
        "Who wins the series?",
        "WHO WILL WIN SERIES",
    ],
)
def test_classifier_recognizes_winner_text(question: str) -> None:
    assert classify_series_sub_type(_market(question=question)) == SeriesSubType.WINNER


@pytest.mark.parametrize(
    "question",
    [
        "Total games over 5.5",
        "NHL Playoffs: Ducks vs. Oilers Total Games O/U 5.5",
        "Games O/U 6.5",
        "Total Games OU 5",
        "Will the series have games over 6.5?",
    ],
)
def test_classifier_recognizes_total_games_text(question: str) -> None:
    assert classify_series_sub_type(_market(question=question)) == SeriesSubType.TOTAL_GAMES


@pytest.mark.parametrize(
    "question",
    [
        "Game 5 handicap: Lakers -3.5",
        "Series handicap: Celtics -2.5",
        "NBA series handicap",
    ],
)
def test_classifier_recognizes_game_handicap_text(question: str) -> None:
    assert classify_series_sub_type(_market(question=question)) == SeriesSubType.GAME_HANDICAP


@pytest.mark.parametrize(
    "question",
    [
        "Will Celtics win 2026 NBA championship?",
        "Lakers vs Knicks moneyline",
        "Total points over 215.5",
        "Will Mahomes throw 3 TDs?",
    ],
)
def test_classifier_returns_other_for_non_series_text(question: str) -> None:
    assert classify_series_sub_type(_market(question=question)) == SeriesSubType.OTHER


def test_classifier_prefers_handicap_over_total_games_when_both_signals_present() -> None:
    # 同时含 "total games" 与 "handicap"——按 deterministic 优先级 handicap > total_games。
    market = _market(question="NBA: Game 5 handicap -3.5 (total games threshold inside)")
    assert classify_series_sub_type(market) == SeriesSubType.GAME_HANDICAP
