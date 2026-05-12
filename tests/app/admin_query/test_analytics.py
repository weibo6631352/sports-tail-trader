"""AdminAnalyticsQueryMixin 直接 unit 测试。

覆盖：
- ``edge_realization_snapshot`` 无 DB 时返回空 items + 空 buckets
- ``pnl_breakdown_snapshot`` 非法 group_by 抛 ValueError
- ``pnl_breakdown_snapshot`` 无 DB 时返回空 rows
- ``missed_opportunities_snapshot`` 无 DB 时走 build_missed_opportunities([])
- ``calibration_snapshot`` 无 DB 时返回空骨架
- ``latency_percentiles_snapshot`` 无 DB 时返回空 stage 骨架
- ``portfolio_risk_metrics`` 无 DB 时抛 RuntimeError
- ``portfolio_equity_curve`` 无 DB 时抛 RuntimeError
- ``run_parameter_sweep`` 无 DB 时仍返回 build_parameter_sweep 投影
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from polymarket_trader.app.admin_query.analytics import AdminAnalyticsQueryMixin


@dataclass
class _Host(AdminAnalyticsQueryMixin):
    has_db: bool = False
    runtime: Any = None

    def _has_db_session_factory(self) -> bool:
        return self.has_db

    async def _with_repositories(self, callback: Callable[[Any], Any]) -> Any:
        raise AssertionError("DB path should not be hit when has_db=False")


def test_edge_realization_snapshot_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.edge_realization_snapshot(limit=200))
    assert payload["items"] == []
    assert payload["limit"] == 200
    assert isinstance(payload["buckets"], list)


def test_pnl_breakdown_snapshot_rejects_unknown_group_by() -> None:
    host = _Host(has_db=False)
    with pytest.raises(ValueError, match="unsupported group_by"):
        asyncio.run(host.pnl_breakdown_snapshot(group_by="weather"))


def test_pnl_breakdown_snapshot_empty_without_db_for_valid_group_by() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.pnl_breakdown_snapshot(group_by="strategy_id"))
    assert payload["group_by"] == "strategy_id"
    assert payload["rows"] == []
    assert "totals" in payload


def test_missed_opportunities_snapshot_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.missed_opportunities_snapshot(limit=50))
    # build_missed_opportunities 在空输入上至少返回稳定结构
    assert isinstance(payload, dict)


def test_calibration_snapshot_returns_empty_skeleton_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.calibration_snapshot(bucket_size=Decimal("0.1")))
    assert payload == {
        "bucket_size": "0.1",
        "buckets": [],
        "brier_score": None,
        "log_loss": None,
        "total_samples": 0,
        "with_outcome_count": 0,
    }


def test_latency_percentiles_snapshot_empty_payload_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.latency_percentiles_snapshot(window_ms=60_000, sample_limit=10))
    assert payload["sample_count"] == 0
    assert payload["sample_limit"] == 10
    assert payload["window_ms"] == 60_000
    assert "queue_to_sign" in payload["stages"]
    assert payload["stages"]["queue_to_sign"]["count"] == 0


def test_latency_percentiles_uses_supplied_event_types() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(
        host.latency_percentiles_snapshot(
            sample_limit=5,
            event_types=("custom_event",),
        )
    )
    assert payload["event_types"] == ["custom_event"]


def test_portfolio_risk_metrics_raises_without_db() -> None:
    host = _Host(has_db=False)
    with pytest.raises(RuntimeError, match="db_session_factory unavailable"):
        asyncio.run(host.portfolio_risk_metrics(window_ms=60_000, interval_ms=10_000))


def test_portfolio_equity_curve_raises_without_db() -> None:
    host = _Host(has_db=False)
    with pytest.raises(RuntimeError, match="db_session_factory unavailable"):
        asyncio.run(host.portfolio_equity_curve(window_ms=60_000, interval_ms=10_000))


def test_run_parameter_sweep_uses_valid_candidate_key_without_db() -> None:
    # 即使无 DB（决策/结算空集），sweep 也应基于 candidates 笛卡尔积返回稳定 envelope
    host = _Host(has_db=False, runtime=SimpleNamespace())
    payload = asyncio.run(
        host.run_parameter_sweep(
            candidates={"entry_no_price_max": [0.05, 0.1]},
            per_decision_usdc=Decimal("10"),
        )
    )
    assert isinstance(payload, dict)
    assert "results" in payload
    # 两个候选值 → 两组结果
    assert len(payload["results"]) == 2
