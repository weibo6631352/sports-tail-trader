"""resolve_series_team：team_a/b 反向匹配 + YES/NO 文本反推。"""

from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from strategies.current.series.team_resolver import resolve_series_team
from strategies.current.series.types import SeriesState


_NOW = datetime(2026, 5, 13, tzinfo=timezone.utc)


def _state() -> SeriesState:
    return SeriesState(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        wins_a=2,
        wins_b=1,
        best_of=7,
        next_game_at=None,
        observed_at=_NOW,
    )


def _market(question: str, *, outcome: str, event_title: str | None = None) -> Market:
    return Market(
        condition_id="cond-series",
        market_slug="slug",
        market_question=question,
        event_title=event_title or question,
        category="Sports",
        tags=("NBA",),
        outcomes=(MarketOutcome(token_id="tok", outcome=outcome),),
        trading_status=TradingStatus.ELIGIBLE,
    )


def test_outcome_label_directly_matches_team_a() -> None:
    market = _market("Will Celtics win the series?", outcome="Celtics")
    side = resolve_series_team(market, "Celtics", _state())
    assert side == "team_a"


def test_outcome_label_directly_matches_team_b() -> None:
    market = _market("Series winner", outcome="Knicks")
    side = resolve_series_team(market, "Knicks", _state())
    assert side == "team_b"


def test_yes_outcome_resolves_to_team_a_from_question_text() -> None:
    market = _market("Will Celtics win the series?", outcome="Yes")
    side = resolve_series_team(market, "Yes", _state())
    assert side == "team_a"


def test_no_outcome_resolves_to_team_b_from_question_text() -> None:
    market = _market("Will Celtics win the series?", outcome="No")
    side = resolve_series_team(market, "No", _state())
    # NO 表示 Celtics 不赢，即 Knicks 赢
    assert side == "team_b"


def test_yes_outcome_pointing_to_team_b_returns_team_b() -> None:
    market = _market("Will Knicks win the series?", outcome="Yes")
    side = resolve_series_team(market, "Yes", _state())
    assert side == "team_b"


def test_ambiguous_text_returns_none() -> None:
    # 两支队都出现在问题中 → 歧义
    market = _market(
        "Celtics vs Knicks: who wins the series?",
        outcome="Yes",
    )
    side = resolve_series_team(market, "Yes", _state())
    assert side is None


def test_no_team_match_returns_none() -> None:
    market = _market(
        "Will Lakers win the series?",
        outcome="Yes",
    )
    side = resolve_series_team(market, "Yes", _state())
    assert side is None


def test_last_token_match_works() -> None:
    # outcome 用 last token；归一化后 last token 与 team_a 末尾匹配。
    market = _market("Series winner", outcome="celtics")
    side = resolve_series_team(market, "celtics", _state())
    assert side == "team_a"
