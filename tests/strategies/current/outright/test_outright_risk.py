from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from strategies.current.outright.risk import check_outright_entry_risk
from strategies.current.outright.types import OutrightRejectReason

_NOW = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)


def _risk(
    *,
    market_end_at=_NOW + timedelta(days=30),
    proposed_amount_usdc=Decimal("10"),
    existing_outright_exposure_usdc=Decimal("0"),
    existing_event_exposure_usdc=Decimal("0"),
    max_per_market_usdc=Decimal("25"),
    max_event_correlation_usdc=Decimal("40"),
    max_total_outright_usdc=Decimal("100"),
    max_hold_horizon_days=180,
    min_remaining_days=0,
):
    return check_outright_entry_risk(
        now=_NOW,
        market_end_at=market_end_at,
        proposed_amount_usdc=proposed_amount_usdc,
        existing_outright_exposure_usdc=existing_outright_exposure_usdc,
        existing_event_exposure_usdc=existing_event_exposure_usdc,
        max_per_market_usdc=max_per_market_usdc,
        max_event_correlation_usdc=max_event_correlation_usdc,
        max_total_outright_usdc=max_total_outright_usdc,
        max_hold_horizon_days=max_hold_horizon_days,
        min_remaining_days=min_remaining_days,
    )


def test_risk_passes_within_all_caps() -> None:
    assert _risk() is None


def test_risk_rejects_when_horizon_exceeded() -> None:
    assert _risk(market_end_at=_NOW + timedelta(days=365), max_hold_horizon_days=180) == OutrightRejectReason.HOLD_HORIZON_EXCEEDED


def test_risk_rejects_when_market_end_already_passed() -> None:
    assert _risk(market_end_at=_NOW - timedelta(hours=1)) == OutrightRejectReason.MARKET_END_PASSED


def test_risk_rejects_when_per_market_cap_breached() -> None:
    assert _risk(proposed_amount_usdc=Decimal("30"), max_per_market_usdc=Decimal("25")) == OutrightRejectReason.PER_MARKET_CAP_EXCEEDED


def test_risk_rejects_when_event_correlation_cap_breached() -> None:
    assert _risk(existing_event_exposure_usdc=Decimal("30"), proposed_amount_usdc=Decimal("15"), max_event_correlation_usdc=Decimal("40")) == OutrightRejectReason.EVENT_CORRELATION_CAP


def test_risk_rejects_when_total_outright_cap_breached() -> None:
    assert _risk(existing_outright_exposure_usdc=Decimal("90"), proposed_amount_usdc=Decimal("20"), max_total_outright_usdc=Decimal("100")) == OutrightRejectReason.TOTAL_BUDGET_EXHAUSTED


def test_risk_accepts_when_market_end_none() -> None:
    # 没有 end date 时（罕见但合法）按 horizon 与累计敞口判定。
    assert _risk(market_end_at=None) is None


def test_risk_rejects_when_min_remaining_days_not_met() -> None:
    # Market ends in 5 days but we require 7 minimum.
    assert _risk(market_end_at=_NOW + timedelta(days=5), min_remaining_days=7) == OutrightRejectReason.MIN_REMAINING_DAYS_NOT_MET


def test_risk_accepts_when_min_remaining_days_exactly_met() -> None:
    # Exactly 7 days remaining meets the requirement (>= 7 days * 86400 seconds).
    assert _risk(market_end_at=_NOW + timedelta(days=7, seconds=1), min_remaining_days=7) is None


def test_risk_accepts_when_min_remaining_days_zero() -> None:
    # min_remaining_days=0 disables the check even for very close expiry.
    assert _risk(market_end_at=_NOW + timedelta(hours=1), min_remaining_days=0) is None


def test_risk_min_remaining_days_not_checked_when_market_end_none() -> None:
    # No market_end_at → min_remaining_days check is skipped.
    assert _risk(market_end_at=None, min_remaining_days=7) is None
