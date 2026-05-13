"""parse_total_games_outcome: 类别型 Over/Under outcome 与 YES/NO 反向匹配。"""

from __future__ import annotations

from decimal import Decimal

from strategies.current.series.total_games_outcome import parse_total_games_outcome


def test_categorical_over_half_line() -> None:
    parsed = parse_total_games_outcome("Over 5.5", market_question="Total Games O/U 5.5")
    assert parsed == (Decimal("5.5"), "over")


def test_categorical_under_integer_line() -> None:
    parsed = parse_total_games_outcome("Under 6", market_question="Total Games O/U 6")
    assert parsed == (Decimal("6"), "under")


def test_categorical_with_extra_label_text() -> None:
    parsed = parse_total_games_outcome("Over 4.5 games", market_question=None)
    assert parsed == (Decimal("4.5"), "over")


def test_binary_yes_inherits_question_direction() -> None:
    parsed = parse_total_games_outcome("Yes", market_question="Will the series go over 5.5 games?")
    assert parsed == (Decimal("5.5"), "over")


def test_binary_no_inverts_question_direction() -> None:
    parsed = parse_total_games_outcome("No", market_question="Will the series go over 5.5 games?")
    # NO 翻转 over → under
    assert parsed == (Decimal("5.5"), "under")


def test_binary_no_with_under_question_inverts_to_over() -> None:
    parsed = parse_total_games_outcome("No", market_question="Will the series go under 6 games?")
    assert parsed == (Decimal("6"), "over")


def test_unknown_outcome_returns_none() -> None:
    parsed = parse_total_games_outcome("Maybe", market_question="Total games TBD")
    assert parsed is None


def test_binary_without_question_direction_returns_none() -> None:
    # YES/NO 但 question 无 over/under → 无法解析，避免乱猜
    parsed = parse_total_games_outcome("Yes", market_question="Will the series go to 7 games?")
    assert parsed is None


def test_empty_inputs_return_none() -> None:
    assert parse_total_games_outcome("", "") is None
