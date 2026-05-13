"""parse_handicap_outcome：类别型 ``Team ±X`` 与 YES/NO 反向匹配 + scope 判定。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from strategies.current.series.handicap_outcome import parse_handicap_outcome
from strategies.current.series.types import SeriesState


_NOW = datetime(2026, 5, 13, tzinfo=timezone.utc)


def _state() -> SeriesState:
    return SeriesState(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        wins_a=0,
        wins_b=0,
        best_of=7,
        next_game_at=None,
        observed_at=_NOW,
    )


def _market(question: str, *, market_name: str = "") -> Market:
    return Market(
        condition_id="cond-handicap",
        market_slug="celtics-handicap",
        market_question=question,
        market_name=market_name,
        event_title="NBA Playoffs Game 5 Handicap",
        category="Sports",
        tags=("NBA",),
        outcomes=(MarketOutcome(token_id="tok", outcome="Yes"),),
        trading_status=TradingStatus.ELIGIBLE,
    )


def test_categorical_team_minus_line_series_scope() -> None:
    market = _market("Boston Celtics series handicap -1.5")
    bet = parse_handicap_outcome("Celtics -1.5", market, _state())
    assert bet is not None
    assert bet.team_side == "team_a"
    assert bet.handicap == Decimal("-1.5")
    assert bet.scope == "series"


def test_categorical_team_plus_line_single_game_scope() -> None:
    market = _market("Knicks +3.5 in Game 5 spread")
    bet = parse_handicap_outcome("Knicks +3.5", market, _state())
    assert bet is not None
    assert bet.team_side == "team_b"
    assert bet.handicap == Decimal("3.5")
    assert bet.scope == "single_game"


def test_binary_yes_resolves_team_and_line_from_question() -> None:
    market = _market("Will the Celtics cover -1.5 games in the series?")
    bet = parse_handicap_outcome("Yes", market, _state())
    assert bet is not None
    assert bet.team_side == "team_a"
    assert bet.handicap == Decimal("-1.5")
    assert bet.scope == "series"


def test_binary_no_flips_line_sign() -> None:
    market = _market("Will the Celtics cover -1.5 games in the series?")
    bet = parse_handicap_outcome("No", market, _state())
    assert bet is not None
    assert bet.team_side == "team_a"  # team 不变
    assert bet.handicap == Decimal("1.5")  # 符号翻转


def test_binary_with_game_n_question_yields_single_game_scope() -> None:
    market = _market("Will the Celtics cover -3.5 in Game 5?")
    bet = parse_handicap_outcome("Yes", market, _state())
    assert bet is not None
    assert bet.scope == "single_game"


def test_unparseable_outcome_returns_none() -> None:
    market = _market("Lakers handicap mystery")  # 没有 Celtics/Knicks
    assert parse_handicap_outcome("Maybe", market, _state()) is None


def test_binary_without_line_in_question_returns_none() -> None:
    market = _market("Will the Celtics win the series?")  # 没有 +/- 数字
    assert parse_handicap_outcome("Yes", market, _state()) is None


def test_last_token_fallback_matches_short_team_name() -> None:
    # 类别型 "Celtics -2.5" 应能匹配到 "Boston Celtics"
    market = _market("Series handicap")
    bet = parse_handicap_outcome("Celtics -2.5", market, _state())
    assert bet is not None
    assert bet.team_side == "team_a"
    assert bet.handicap == Decimal("-2.5")
