from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from strategies.current.outright.risk import check_outright_entry_risk
from strategies.current.outright.types import OutrightRejectReason

_NOW = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)


def test_risk_passes_within_all_caps() -> None:
    assert check_outright_entry_risk(
        now=_NOW,
        market_end_at=_NOW + timedelta(days=30),
        proposed_amount_usdc=Decimal("10"),
        existing_outright_exposure_usdc=Decimal("0"),
        existing_event_exposure_usdc=Decimal("0"),
        max_per_market_usdc=Decimal("25"),
        max_event_correlation_usdc=Decimal("40"),
        max_total_outright_usdc=Decimal("100"),
        max_hold_horizon_days=180,
    ) is None


def test_risk_rejects_when_horizon_exceeded() -> None:
    reason = check_outright_entry_risk(
        now=_NOW,
        market_end_at=_NOW + timedelta(days=365),
        proposed_amount_usdc=Decimal("10"),
        existing_outright_exposure_usdc=Decimal("0"),
        existing_event_exposure_usdc=Decimal("0"),
        max_per_market_usdc=Decimal("25"),
        max_event_correlation_usdc=Decimal("40"),
        max_total_outright_usdc=Decimal("100"),
        max_hold_horizon_days=180,
    )
    assert reason == OutrightRejectReason.HOLD_HORIZON_EXCEEDED


def test_risk_rejects_when_market_end_already_passed() -> None:
    reason = check_outright_entry_risk(
        now=_NOW,
        market_end_at=_NOW - timedelta(hours=1),
        proposed_amount_usdc=Decimal("10"),
        existing_outright_exposure_usdc=Decimal("0"),
        existing_event_exposure_usdc=Decimal("0"),
        max_per_market_usdc=Decimal("25"),
        max_event_correlation_usdc=Decimal("40"),
        max_total_outright_usdc=Decimal("100"),
        max_hold_horizon_days=180,
    )
    assert reason == OutrightRejectReason.MARKET_END_PASSED


def test_risk_rejects_when_per_market_cap_breached() -> None:
    reason = check_outright_entry_risk(
        now=_NOW,
        market_end_at=_NOW + timedelta(days=30),
        proposed_amount_usdc=Decimal("30"),
        existing_outright_exposure_usdc=Decimal("0"),
        existing_event_exposure_usdc=Decimal("0"),
        max_per_market_usdc=Decimal("25"),
        max_event_correlation_usdc=Decimal("40"),
        max_total_outright_usdc=Decimal("100"),
        max_hold_horizon_days=180,
    )
    assert reason == OutrightRejectReason.PER_MARKET_CAP_EXCEEDED


def test_risk_rejects_when_event_correlation_cap_breached() -> None:
    reason = check_outright_entry_risk(
        now=_NOW,
        market_end_at=_NOW + timedelta(days=30),
        proposed_amount_usdc=Decimal("15"),
        existing_outright_exposure_usdc=Decimal("0"),
        existing_event_exposure_usdc=Decimal("30"),
        max_per_market_usdc=Decimal("25"),
        max_event_correlation_usdc=Decimal("40"),
        max_total_outright_usdc=Decimal("100"),
        max_hold_horizon_days=180,
    )
    assert reason == OutrightRejectReason.EVENT_CORRELATION_CAP


def test_risk_rejects_when_total_outright_cap_breached() -> None:
    reason = check_outright_entry_risk(
        now=_NOW,
        market_end_at=_NOW + timedelta(days=30),
        proposed_amount_usdc=Decimal("20"),
        existing_outright_exposure_usdc=Decimal("90"),
        existing_event_exposure_usdc=Decimal("0"),
        max_per_market_usdc=Decimal("25"),
        max_event_correlation_usdc=Decimal("40"),
        max_total_outright_usdc=Decimal("100"),
        max_hold_horizon_days=180,
    )
    assert reason == OutrightRejectReason.TOTAL_BUDGET_EXHAUSTED


def test_risk_accepts_when_market_end_none() -> None:
    # 没有 end date 时（罕见但合法）按 horizon 与累计敞口判定。
    assert check_outright_entry_risk(
        now=_NOW,
        market_end_at=None,
        proposed_amount_usdc=Decimal("10"),
        existing_outright_exposure_usdc=Decimal("0"),
        existing_event_exposure_usdc=Decimal("0"),
        max_per_market_usdc=Decimal("25"),
        max_event_correlation_usdc=Decimal("40"),
        max_total_outright_usdc=Decimal("100"),
        max_hold_horizon_days=180,
    ) is None
