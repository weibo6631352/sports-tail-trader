"""组合净值时间序列服务。

只负责把 ``account_snapshots`` 时间序列查回内存、做 drawdown 投影。所有
downsampling 已经在仓储里靠 PG ``date_trunc/floor(epoch)`` 完成；服务侧只
处理少量已聚合点，不在交易主链路上跑。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from polymarket_trader.domain.account import AccountHistoryPoint


# 默认窗口 7d、采样 60s——和 60s 后台 snapshot job 对齐。
DEFAULT_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
DEFAULT_INTERVAL_MS = 60_000
MIN_INTERVAL_MS = 1_000
MAX_WINDOW_MS = 365 * 24 * 60 * 60 * 1000


HistoryQuery = Callable[..., Awaitable[tuple[AccountHistoryPoint, ...]]]


@dataclass(frozen=True, slots=True)
class EquityCurveResult:
    window_ms: int
    interval_ms: int
    points: tuple[AccountHistoryPoint, ...]
    max_drawdown_pct: Decimal
    peak_at: datetime | None
    trough_at: datetime | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "window_ms": self.window_ms,
            "interval_ms": self.interval_ms,
            "points": [
                {
                    "ts": point.recorded_at.astimezone(timezone.utc).isoformat(),
                    "net_usdc": str(point.net_value_usdc),
                }
                for point in self.points
            ],
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "peak_at": (
                None
                if self.peak_at is None
                else self.peak_at.astimezone(timezone.utc).isoformat()
            ),
            "trough_at": (
                None
                if self.trough_at is None
                else self.trough_at.astimezone(timezone.utc).isoformat()
            ),
            "sample_size": len(self.points),
        }


class PortfolioHistoryService:
    """读 ``account_snapshots`` 时间序列、计算 drawdown 投影。"""

    def __init__(
        self,
        *,
        query_history: HistoryQuery,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._query_history = query_history
        self._now = now_provider or (lambda: datetime.now(timezone.utc))

    async def equity_curve(
        self,
        *,
        window_ms: int = DEFAULT_WINDOW_MS,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        account_key: str = "primary",
    ) -> EquityCurveResult:
        window_ms = _clamp_window_ms(window_ms)
        interval_ms = _clamp_interval_ms(interval_ms, window_ms)
        until = self._now().astimezone(timezone.utc)
        since = until - timedelta(milliseconds=window_ms)
        points = await self._query_history(
            since=since,
            until=until,
            interval_ms=interval_ms,
            account_key=account_key,
        )
        max_dd, peak_at, trough_at = compute_max_drawdown(points)
        return EquityCurveResult(
            window_ms=window_ms,
            interval_ms=interval_ms,
            points=tuple(points),
            max_drawdown_pct=max_dd,
            peak_at=peak_at,
            trough_at=trough_at,
        )


def compute_max_drawdown(
    points: tuple[AccountHistoryPoint, ...] | list[AccountHistoryPoint],
) -> tuple[Decimal, datetime | None, datetime | None]:
    """走一遍 downsampled 序列，返回 ``(max_drawdown_pct, peak_at, trough_at)``。

    drawdown = ``(peak - trough) / peak``，要求 peak > 0，否则跳过该 peak。
    单调上升或空序列返回 ``(0, None, None)``。
    """

    running_peak: Decimal | None = None
    running_peak_at: datetime | None = None
    max_dd = Decimal("0")
    best_peak_at: datetime | None = None
    best_trough_at: datetime | None = None

    for point in points:
        value = point.net_value_usdc
        if running_peak is None or value > running_peak:
            running_peak = value
            running_peak_at = point.recorded_at
            continue
        if running_peak is None or running_peak <= Decimal("0"):
            continue
        drawdown = (running_peak - value) / running_peak
        if drawdown > max_dd:
            max_dd = drawdown
            best_peak_at = running_peak_at
            best_trough_at = point.recorded_at

    return max_dd, best_peak_at, best_trough_at


def _clamp_window_ms(window_ms: int) -> int:
    if window_ms <= 0:
        return DEFAULT_WINDOW_MS
    return min(window_ms, MAX_WINDOW_MS)


def _clamp_interval_ms(interval_ms: int, window_ms: int) -> int:
    if interval_ms <= 0:
        return DEFAULT_INTERVAL_MS
    # 限制最低粒度避免一次返回百万点；上限不超过窗口本身。
    return max(MIN_INTERVAL_MS, min(interval_ms, max(window_ms, MIN_INTERVAL_MS)))


__all__ = [
    "DEFAULT_INTERVAL_MS",
    "DEFAULT_WINDOW_MS",
    "EquityCurveResult",
    "PortfolioHistoryService",
    "compute_max_drawdown",
]
