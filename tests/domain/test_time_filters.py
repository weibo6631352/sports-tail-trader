"""Domain ``TimeRange`` 值对象覆盖：构造校验、转换、范围判定。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polymarket_trader.domain.time_filters import TimeRange


def test_unbounded_range_is_empty_and_contains_anything() -> None:
    time_range = TimeRange()
    assert time_range.is_empty
    # 任选一个固定瞬间断言"无界区间包含任意时刻"。
    assert time_range.contains(datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc))
    # 空区间也允许 None 时刻（无需过滤）。
    assert time_range.contains(None)
    since_dt, until_dt = time_range.to_datetime_range()
    assert since_dt is None and until_dt is None


def test_since_greater_than_until_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="since_after_until"):
        TimeRange(since_ms=2_000, until_ms=1_000)


def test_equal_bounds_are_allowed_as_single_point_window() -> None:
    time_range = TimeRange(since_ms=1_000, until_ms=1_000)
    assert not time_range.is_empty
    boundary = datetime.fromtimestamp(1, tz=timezone.utc)
    assert time_range.contains(boundary)


def test_to_datetime_range_converts_epoch_ms_to_utc() -> None:
    time_range = TimeRange(since_ms=1_500, until_ms=2_500)
    since_dt, until_dt = time_range.to_datetime_range()
    assert since_dt == datetime(1970, 1, 1, 0, 0, 1, 500_000, tzinfo=timezone.utc)
    assert until_dt == datetime(1970, 1, 1, 0, 0, 2, 500_000, tzinfo=timezone.utc)


def test_contains_uses_closed_interval_semantics() -> None:
    time_range = TimeRange(since_ms=1_000, until_ms=3_000)
    lower = datetime.fromtimestamp(1, tz=timezone.utc)
    upper = datetime.fromtimestamp(3, tz=timezone.utc)
    inside = datetime.fromtimestamp(2, tz=timezone.utc)
    before = datetime.fromtimestamp(0, tz=timezone.utc)
    after = datetime.fromtimestamp(4, tz=timezone.utc)
    assert time_range.contains(lower)
    assert time_range.contains(upper)
    assert time_range.contains(inside)
    assert not time_range.contains(before)
    assert not time_range.contains(after)


def test_contains_treats_none_moment_as_excluded_when_bounded() -> None:
    # 任一端有界即视为有界窗口；None 时刻无法证明落在窗内 -> 剔除。
    bounded_since = TimeRange(since_ms=1_000)
    bounded_until = TimeRange(until_ms=2_000)
    assert not bounded_since.contains(None)
    assert not bounded_until.contains(None)


def test_only_since_means_open_upper_bound() -> None:
    time_range = TimeRange(since_ms=1_000)
    assert not time_range.is_empty
    far_future = datetime(2999, 1, 1, tzinfo=timezone.utc)
    assert time_range.contains(far_future)
    assert not time_range.contains(datetime.fromtimestamp(0, tz=timezone.utc))


def test_only_until_means_open_lower_bound() -> None:
    time_range = TimeRange(until_ms=1_000)
    assert not time_range.is_empty
    far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert time_range.contains(far_past)
    assert not time_range.contains(datetime.fromtimestamp(2, tz=timezone.utc))


def test_frozen_dataclass_is_hashable_and_immutable() -> None:
    time_range = TimeRange(since_ms=1_000, until_ms=2_000)
    # frozen=True -> hashable，可用作字典 key 或集合元素。
    {time_range}
    with pytest.raises(Exception):  # FrozenInstanceError
        time_range.since_ms = 5_000  # type: ignore[misc]
