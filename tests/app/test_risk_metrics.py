"""``build_risk_metrics`` 数学正确性。

覆盖：
- 空/单点序列安全返回 None
- max_drawdown 计算
- time_underwater 累计
- simple returns 均值 + stddev
- Sharpe-like = mean/std；带 annualization 时 = mean/std * sqrt(factor)
- total_return = (end - start) / start
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_trader.app.risk_metrics import build_risk_metrics
from polymarket_trader.infra.db import AccountHistoryPoint


BASE = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


def _point(offset_seconds: int, value: str) -> AccountHistoryPoint:
    return AccountHistoryPoint(
        recorded_at=BASE + timedelta(seconds=offset_seconds),
        net_value_usdc=Decimal(value),
    )


def test_empty_series_returns_none_metrics() -> None:
    metrics = build_risk_metrics(())
    assert metrics["sample_count"] == 0
    assert metrics["sharpe_like"] is None
    assert metrics["max_drawdown_pct"] == "0"


def test_single_point_returns_none_metrics() -> None:
    metrics = build_risk_metrics((_point(0, "100"),))
    assert metrics["sample_count"] == 1
    assert metrics["mean_return_per_period"] is None
    assert metrics["sharpe_like"] is None


def test_max_drawdown_picks_largest_peak_to_trough() -> None:
    points = (
        _point(0, "100"),
        _point(60, "120"),  # peak
        _point(120, "90"),  # trough → drawdown = 30/120 = 25%
        _point(180, "95"),
        _point(240, "150"),
    )
    metrics = build_risk_metrics(points)
    assert Decimal(metrics["max_drawdown_pct"]) == Decimal("0.25")


def test_time_underwater_accumulates_periods_below_peak() -> None:
    points = (
        _point(0, "100"),
        _point(60, "100"),
        _point(120, "95"),  # underwater starts
        _point(180, "90"),  # still underwater
        _point(240, "105"),  # new peak; underwater clears
    )
    metrics = build_risk_metrics(points)
    # underwater 区间：点 [120, 240]，即 t=180 和 t=240 区间结束时清掉。
    # 算法：prev_underwater 在 120 时为 True（因为 95 < 100），所以 120→180
    # 的 60 秒计入，180→240 的 60 秒也计入（180 时 90 还在 underwater）。
    # 240 时变成新 peak 不在 underwater；但累计已经是 120 秒。
    assert metrics["time_underwater_seconds"] == 120


def test_total_return_simple_formula() -> None:
    points = (_point(0, "100"), _point(60, "150"))
    metrics = build_risk_metrics(points)
    assert float(metrics["total_return"]) == 0.5


def test_sharpe_like_with_constant_returns_is_infinite_so_none() -> None:
    # 完全线性增长：每步 return 都一样 → stddev = 0 → Sharpe 不可算 → None
    points = (_point(0, "100"), _point(60, "110"), _point(120, "121"))
    metrics = build_risk_metrics(points)
    assert metrics["sharpe_like"] is None


def test_sharpe_like_with_variable_returns() -> None:
    points = (
        _point(0, "100"),
        _point(60, "110"),  # +10%
        _point(120, "99"),  # -10%
        _point(180, "108.9"),  # +10%
    )
    metrics = build_risk_metrics(points)
    # 收益 = [0.10, -0.10, 0.10]
    # mean = 0.0333..., stddev (样本) = sqrt(0.0133.../2) ≈ 0.1155
    # sharpe ≈ 0.2887
    assert metrics["sharpe_like"] is not None
    assert float(metrics["sharpe_like"]) == pytest_approx(0.2887, abs=0.01)


def test_sharpe_like_annualized_scales_by_sqrt_factor() -> None:
    points = (
        _point(0, "100"),
        _point(60, "110"),
        _point(120, "99"),
        _point(180, "108.9"),
    )
    base = build_risk_metrics(points)
    annualized = build_risk_metrics(points, annualization_factor=365)
    base_sharpe = float(base["sharpe_like"])
    annual_sharpe = float(annualized["sharpe_like"])
    assert annual_sharpe == pytest_approx(base_sharpe * math.sqrt(365), abs=0.1)


# 轻量 approx——避免引入 pytest 模块级 fixture
class _Approx:
    def __init__(self, target: float, abs_tol: float) -> None:
        self.target = target
        self.abs_tol = abs_tol

    def __eq__(self, other: object) -> bool:
        return isinstance(other, (int, float)) and abs(other - self.target) <= self.abs_tol

    def __repr__(self) -> str:
        return f"≈{self.target}±{self.abs_tol}"


def pytest_approx(value: float, *, abs: float) -> _Approx:  # noqa: A002 - 风格匹配 pytest
    return _Approx(value, abs)
