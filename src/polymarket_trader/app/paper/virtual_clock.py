"""沙箱可注入时钟。

只在 shadow_runner 路径使用；生产代码完全不感知（不引入只服务过渡期的 runtime
状态字段，按 §8）。三个实现：
- ``RealClock``：包一层 ``datetime.now`` / ``time.monotonic``，单元测试不需要时退化用
- ``EventTimestampClock``：跟随当前正在处理的 ShadowEvent 时间戳推进
- ``FrozenClock``：构造时固定时间，``advance(seconds)`` 显式推进
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol


class VirtualClock(Protocol):
    """沙箱时钟接口，便于把"现在"显式注入到 worker 与状态判定。"""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


@dataclass(slots=True)
class RealClock:
    """真实时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(slots=True)
class FrozenClock:
    """固定时钟，``advance`` 显式推进，便于单元测试做确定性断言。"""

    _current: datetime
    _monotonic_seconds: float = 0.0

    def __init__(self, start: datetime) -> None:
        self._current = start if start.tzinfo is not None else start.replace(tzinfo=timezone.utc)
        self._monotonic_seconds = 0.0

    def now(self) -> datetime:
        return self._current

    def monotonic(self) -> float:
        return self._monotonic_seconds

    def advance(self, seconds: float) -> None:
        self._current = self._current + timedelta(seconds=seconds)
        self._monotonic_seconds += seconds


@dataclass(slots=True)
class EventTimestampClock:
    """跟随 ShadowEvent 时间戳推进；shadow_runner 在每个事件前调 ``set``。"""

    _current: datetime = field(
        default_factory=lambda: datetime.fromtimestamp(0, tz=timezone.utc)
    )
    _baseline: datetime = field(
        default_factory=lambda: datetime.fromtimestamp(0, tz=timezone.utc)
    )

    def now(self) -> datetime:
        return self._current

    def monotonic(self) -> float:
        return (self._current - self._baseline).total_seconds()

    def set(self, observed_at: datetime) -> None:
        normalized = observed_at if observed_at.tzinfo is not None else observed_at.replace(tzinfo=timezone.utc)
        if self._baseline == datetime.fromtimestamp(0, tz=timezone.utc):
            self._baseline = normalized
        self._current = normalized
