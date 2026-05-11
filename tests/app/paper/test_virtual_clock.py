"""沙箱时钟单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trader.app.paper.virtual_clock import (
    EventTimestampClock,
    FrozenClock,
    RealClock,
)


def test_frozen_clock_advance() -> None:
    clock = FrozenClock(datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc))
    assert clock.now() == datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    assert clock.monotonic() == 0.0
    clock.advance(60)
    assert clock.now() == datetime(2026, 5, 11, 12, 1, 0, tzinfo=timezone.utc)
    assert clock.monotonic() == 60.0


def test_frozen_clock_naive_datetime_treated_as_utc() -> None:
    clock = FrozenClock(datetime(2026, 5, 11, 12, 0, 0))
    assert clock.now().tzinfo == timezone.utc


def test_event_timestamp_clock_tracks_set() -> None:
    clock = EventTimestampClock()
    t0 = datetime(2026, 5, 11, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=120)
    clock.set(t0)
    assert clock.now() == t0
    assert clock.monotonic() == 0.0
    clock.set(t1)
    assert clock.now() == t1
    assert clock.monotonic() == 120.0


def test_event_timestamp_clock_normalizes_naive_input() -> None:
    clock = EventTimestampClock()
    naive = datetime(2026, 5, 11, 12, 0, 0)
    clock.set(naive)
    assert clock.now().tzinfo == timezone.utc


def test_real_clock_returns_aware_datetime() -> None:
    clock = RealClock()
    now = clock.now()
    assert now.tzinfo == timezone.utc
    assert clock.monotonic() >= 0
