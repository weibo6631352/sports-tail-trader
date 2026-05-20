"""Outright capital efficiency gate unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from strategies.current.outright.decide import _outright_capital_efficiency_metadata

_NOW = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
_MIN_PER_DAY = Decimal("0.02")


def _gate(
    *,
    amount: str = "10",
    price: str = "0.90",
    days_to_end: float = 30.0,
    min_per_day: str = "0.02",
) -> tuple[bool, dict]:
    end = _NOW + timedelta(days=days_to_end)
    return _outright_capital_efficiency_metadata(
        Decimal(amount),
        Decimal(price),
        end,
        _NOW,
        Decimal(min_per_day),
    )


def test_sufficient_efficiency_passes() -> None:
    # $10 at 0.90 → shares=11.11, profit=1.11, days=31 → 0.036/day > 0.02
    passed, meta = _gate(amount="10", price="0.90", days_to_end=30)
    assert passed
    assert "outright_expected_profit_per_day_usdc" in meta
    assert float(meta["outright_expected_profit_per_day_usdc"]) > 0.02


def test_long_hold_low_amount_fails() -> None:
    # $1 at 0.95 → shares=1.053, profit=0.053, days=121 → 0.00044/day < 0.02
    passed, _ = _gate(amount="1", price="0.95", days_to_end=120)
    assert not passed


def test_short_hold_passes() -> None:
    # $5 at 0.90 → shares=5.55, profit=0.55, days=8 → 0.069/day > 0.02
    passed, _ = _gate(amount="5", price="0.90", days_to_end=7)
    assert passed


def test_no_end_date_passes_through() -> None:
    passed, meta = _outright_capital_efficiency_metadata(
        Decimal("10"), Decimal("0.90"), None, _NOW, _MIN_PER_DAY
    )
    assert passed
    assert meta == {}


def test_entry_price_at_boundary_passes_through() -> None:
    # price >= 1.0 → degenerate, gate skips
    passed, meta = _gate(price="1.00")
    assert passed
    assert meta == {}


def test_entry_price_zero_passes_through() -> None:
    passed, meta = _gate(price="0.00")
    assert passed
    assert meta == {}


def test_metadata_keys_present() -> None:
    _, meta = _gate()
    assert "outright_estimated_settlement_days" in meta
    assert "outright_expected_profit_usdc" in meta
    assert "outright_expected_profit_per_day_usdc" in meta
    assert "outright_min_expected_profit_per_day_usdc" in meta


def test_settlement_days_includes_one_day_buffer() -> None:
    # 30 days to end → settlement_days = 31 (30 + 1 buffer)
    _, meta = _gate(days_to_end=30)
    assert abs(meta["outright_estimated_settlement_days"] - 31.0) < 0.1


def test_exact_threshold_passes() -> None:
    # Find amount where profit/day == min_per_day exactly.
    # days=31, price=0.90 → profit = amount/0.9*0.1 = amount/9
    # profit_per_day = amount/9/31 = 0.02 → amount = 0.02*9*31 = 5.58
    passed, meta = _gate(amount="5.58", price="0.90", days_to_end=30, min_per_day="0.02")
    assert passed
