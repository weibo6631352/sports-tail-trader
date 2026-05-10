from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api.decisions import DecisionKind
from polymarket_trader.extension_api.toolkit import (
    avg_cost,
    build_entry_key,
    clamp,
    exposure_usdc,
    pct_of,
    round_to_tick,
    unrealized_pnl,
)


def test_clamp_respects_bounds() -> None:
    assert clamp(Decimal("0.5"), lower=Decimal("1")) == Decimal("1")
    assert clamp(Decimal("10"), upper=Decimal("5")) == Decimal("5")
    assert clamp(Decimal("3"), lower=Decimal("1"), upper=Decimal("5")) == Decimal("3")
    assert clamp(Decimal("3")) == Decimal("3")


def test_round_to_tick_rounds_down_by_default() -> None:
    assert round_to_tick(Decimal("0.567"), Decimal("0.01")) == Decimal("0.56")
    assert round_to_tick(Decimal("0.567"), Decimal("0.05")) == Decimal("0.55")


def test_round_to_tick_zero_tick_is_passthrough() -> None:
    assert round_to_tick(Decimal("0.567"), Decimal("0")) == Decimal("0.567")


def test_pct_of_handles_zero_denominator() -> None:
    assert pct_of(Decimal("5"), Decimal("0")) == Decimal("0")
    assert pct_of(Decimal("5"), Decimal("20")) == Decimal("25")


def test_avg_cost_returns_none_for_empty_position() -> None:
    assert avg_cost(None) is None
    pos = Position(condition_id="c", token_id="t", shares=Decimal("0"), cost_usdc=Decimal("0"))
    assert avg_cost(pos) is None


def test_avg_cost_computes_average() -> None:
    pos = Position(condition_id="c", token_id="t", shares=Decimal("10"), cost_usdc=Decimal("4.5"))
    assert avg_cost(pos) == Decimal("0.45")


def test_unrealized_pnl_uses_mark_price() -> None:
    pos = Position(condition_id="c", token_id="t", shares=Decimal("10"), cost_usdc=Decimal("4.5"))
    assert unrealized_pnl(pos, mark_price=Decimal("0.6")) == Decimal("1.5")


def test_unrealized_pnl_none_when_missing_inputs() -> None:
    assert unrealized_pnl(None, mark_price=Decimal("0.6")) is None
    pos = Position(condition_id="c", token_id="t", shares=Decimal("10"), cost_usdc=Decimal("4.5"))
    assert unrealized_pnl(pos, mark_price=None) is None


def test_exposure_returns_zero_when_missing() -> None:
    assert exposure_usdc(None, mark_price=Decimal("0.5")) == Decimal("0")


def test_build_entry_key_is_deterministic_and_kind_specific() -> None:
    a = build_entry_key(condition_id="c1", token_id="t1", decision_kind=DecisionKind.ENTRY, window="w1")
    b = build_entry_key(condition_id="c1", token_id="t1", decision_kind=DecisionKind.ENTRY, window="w1")
    assert a == b
    other = build_entry_key(
        condition_id="c1", token_id="t1", decision_kind=DecisionKind.SCALE_IN, window="w1"
    )
    assert other != a
    assert other.startswith("entry-scale_in-")
