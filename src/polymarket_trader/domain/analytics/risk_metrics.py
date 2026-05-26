"""组合级风险归因——max drawdown、time underwater、波动率、Sharpe-like。

依赖 ``account_snapshots`` 时间序列（PortfolioHistoryService.equity_curve 已经
downsampled 好），输入 ``AccountHistoryPoint`` 序列输出二阶统计。本模块纯函
数，不做 I/O。

Sharpe-like 注意事项：
- 没有无风险利率，``mean_return / stddev_return`` 直接给出"单位波动收益"；
- 用 simple 收益（``(v_t - v_{t-1}) / v_{t-1}``），不是 log 收益；
- 年化因子由 caller 提供（如 1d 桶则 365）；如果不传则不年化。
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from polymarket_trader.domain.account import AccountHistoryPoint
from polymarket_trader.domain.analytics.portfolio_history_service import compute_max_drawdown


def build_risk_metrics(
    points: Sequence[AccountHistoryPoint] | tuple[AccountHistoryPoint, ...],
    *,
    annualization_factor: float | None = None,
) -> dict[str, Any]:
    """从 equity 时间序列计算风险指标。

    空序列 / 单点序列也安全：相关指标返回 None 而不是抛错。
    """

    points_tuple = tuple(points)
    if len(points_tuple) < 2:
        return _empty_metrics(points_tuple, annualization_factor)

    max_dd, peak_at, trough_at = compute_max_drawdown(points_tuple)
    underwater_seconds = _time_underwater_seconds(points_tuple)
    total_seconds = _total_span_seconds(points_tuple)
    underwater_ratio = (
        underwater_seconds / total_seconds if total_seconds > 0 else None
    )

    returns = _simple_returns(points_tuple)
    mean_return = _mean(returns)
    stddev_return = _stddev(returns, mean_return)
    sharpe = (
        (mean_return / stddev_return)
        if stddev_return is not None and stddev_return > 0
        else None
    )
    if sharpe is not None and annualization_factor is not None:
        sharpe = sharpe * math.sqrt(annualization_factor)

    start_value = points_tuple[0].net_value_usdc
    end_value = points_tuple[-1].net_value_usdc
    total_return = (
        (end_value - start_value) / start_value if start_value > Decimal("0") else None
    )

    return {
        "sample_count": len(points_tuple),
        "first_recorded_at": _iso(points_tuple[0].recorded_at),
        "last_recorded_at": _iso(points_tuple[-1].recorded_at),
        "start_net_value_usdc": str(start_value),
        "end_net_value_usdc": str(end_value),
        "total_return": None if total_return is None else f"{float(total_return):.6f}",
        "max_drawdown_pct": str(max_dd),
        "peak_at": _iso(peak_at),
        "trough_at": _iso(trough_at),
        "time_underwater_seconds": underwater_seconds,
        "time_underwater_ratio": (
            None if underwater_ratio is None else f"{underwater_ratio:.6f}"
        ),
        "mean_return_per_period": None if mean_return is None else f"{mean_return:.8f}",
        "stddev_return_per_period": None if stddev_return is None else f"{stddev_return:.8f}",
        "sharpe_like": None if sharpe is None else f"{sharpe:.6f}",
        "annualization_factor": annualization_factor,
    }


def _empty_metrics(
    points: tuple[AccountHistoryPoint, ...],
    annualization_factor: float | None,
) -> dict[str, Any]:
    return {
        "sample_count": len(points),
        "first_recorded_at": _iso(points[0].recorded_at) if points else None,
        "last_recorded_at": _iso(points[-1].recorded_at) if points else None,
        "start_net_value_usdc": str(points[0].net_value_usdc) if points else None,
        "end_net_value_usdc": str(points[-1].net_value_usdc) if points else None,
        "total_return": None,
        "max_drawdown_pct": "0",
        "peak_at": None,
        "trough_at": None,
        "time_underwater_seconds": 0,
        "time_underwater_ratio": None,
        "mean_return_per_period": None,
        "stddev_return_per_period": None,
        "sharpe_like": None,
        "annualization_factor": annualization_factor,
    }


def _simple_returns(points: tuple[AccountHistoryPoint, ...]) -> list[float]:
    out: list[float] = []
    prev = points[0].net_value_usdc
    for point in points[1:]:
        curr = point.net_value_usdc
        if prev > Decimal("0"):
            out.append(float((curr - prev) / prev))
        prev = curr
    return out


def _time_underwater_seconds(points: tuple[AccountHistoryPoint, ...]) -> int:
    """累计"net_value 低于历史峰值"的总秒数。"""

    underwater = 0
    running_peak: Decimal | None = None
    prev_time: datetime | None = None
    prev_underwater = False
    for point in points:
        if running_peak is None or point.net_value_usdc > running_peak:
            running_peak = point.net_value_usdc
            now_underwater = False
        else:
            now_underwater = point.net_value_usdc < running_peak
        if prev_time is not None and prev_underwater:
            delta = (point.recorded_at - prev_time).total_seconds()
            if delta > 0:
                underwater += int(delta)
        prev_time = point.recorded_at
        prev_underwater = now_underwater
    return underwater


def _total_span_seconds(points: tuple[AccountHistoryPoint, ...]) -> int:
    if len(points) < 2:
        return 0
    return int((points[-1].recorded_at - points[0].recorded_at).total_seconds())


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _stddev(values: list[float], mean: float | None) -> float | None:
    if not values or mean is None or len(values) < 2:
        return None
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
