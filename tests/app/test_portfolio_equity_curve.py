from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polymarket_trader.app.portfolio_history_service import (
    DEFAULT_INTERVAL_MS,
    DEFAULT_WINDOW_MS,
    PortfolioHistoryService,
    compute_max_drawdown,
)
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.infra.db import AccountHistoryPoint
from polymarket_trader.infra.db.models import AccountSnapshotModel


_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _point(offset_minutes: int, net: str) -> AccountHistoryPoint:
    return AccountHistoryPoint(
        recorded_at=_T0 + timedelta(minutes=offset_minutes),
        net_value_usdc=Decimal(net),
    )


def test_compute_max_drawdown_empty_returns_zero() -> None:
    max_dd, peak_at, trough_at = compute_max_drawdown(())
    assert max_dd == Decimal("0")
    assert peak_at is None
    assert trough_at is None


def test_compute_max_drawdown_monotonic_increase_is_zero() -> None:
    points = (_point(0, "100"), _point(1, "110"), _point(2, "120"))
    max_dd, peak_at, trough_at = compute_max_drawdown(points)
    assert max_dd == Decimal("0")
    assert peak_at is None
    assert trough_at is None


def test_compute_max_drawdown_canonical_sequence() -> None:
    # 100 -> 110 -> 90 -> 105：peak 110 → trough 90，drawdown = 20/110 ≈ 0.1818.
    points = (
        _point(0, "100"),
        _point(1, "110"),
        _point(2, "90"),
        _point(3, "105"),
    )
    max_dd, peak_at, trough_at = compute_max_drawdown(points)
    expected = (Decimal("110") - Decimal("90")) / Decimal("110")
    assert max_dd == expected
    assert peak_at == _T0 + timedelta(minutes=1)
    assert trough_at == _T0 + timedelta(minutes=2)
    # 校验四舍五入到小数 4 位的展示形态符合任务期望。
    assert round(max_dd, 4) == Decimal("0.1818")


def test_compute_max_drawdown_skips_non_positive_peak() -> None:
    # peak <= 0 时不能除——直接跳过该 peak。
    points = (_point(0, "0"), _point(1, "-5"), _point(2, "10"), _point(3, "5"))
    max_dd, peak_at, trough_at = compute_max_drawdown(points)
    assert max_dd == Decimal("0.5")
    assert peak_at == _T0 + timedelta(minutes=2)
    assert trough_at == _T0 + timedelta(minutes=3)


@pytest.mark.asyncio
async def test_equity_curve_passes_window_and_returns_payload() -> None:
    captured: dict[str, object] = {}

    async def fake_query(*, since, until, interval_ms, account_key):
        captured.update(
            since=since,
            until=until,
            interval_ms=interval_ms,
            account_key=account_key,
        )
        return (_point(0, "100"), _point(1, "110"), _point(2, "90"), _point(3, "105"))

    frozen_now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    service = PortfolioHistoryService(
        query_history=fake_query,
        now_provider=lambda: frozen_now,
    )
    result = await service.equity_curve(
        window_ms=DEFAULT_WINDOW_MS,
        interval_ms=DEFAULT_INTERVAL_MS,
    )
    assert captured["until"] == frozen_now
    assert captured["since"] == frozen_now - timedelta(milliseconds=DEFAULT_WINDOW_MS)
    assert captured["interval_ms"] == DEFAULT_INTERVAL_MS
    assert captured["account_key"] == "primary"

    payload = result.to_payload()
    assert payload["window_ms"] == DEFAULT_WINDOW_MS
    assert payload["interval_ms"] == DEFAULT_INTERVAL_MS
    assert payload["sample_size"] == 4
    # Decimal 必须以字符串序列化，保住精度。
    assert payload["points"][0] == {
        "ts": "2026-01-01T00:00:00+00:00",
        "net_usdc": "100",
    }
    assert payload["max_drawdown_pct"] == str(
        (Decimal("110") - Decimal("90")) / Decimal("110")
    )
    assert payload["peak_at"] == "2026-01-01T00:01:00+00:00"
    assert payload["trough_at"] == "2026-01-01T00:02:00+00:00"


def test_account_snapshot_model_computes_net_value_from_positions() -> None:
    snapshot = AccountSnapshot(
        balance_usdc=Decimal("250.00"),
        positions=(
            Position(
                condition_id="c1",
                token_id="t1",
                shares=Decimal("100"),
                cost_usdc=Decimal("80"),
                current_value=Decimal("90.50"),
            ),
            Position(
                condition_id="c2",
                token_id="t2",
                shares=Decimal("50"),
                cost_usdc=Decimal("20"),
                current_value=Decimal("12.25"),
            ),
            # 缺少 current_value 的仓位按 0 计——无法在事后插值，避免污染历史。
            Position(
                condition_id="c3",
                token_id="t3",
                shares=Decimal("10"),
                cost_usdc=Decimal("5"),
            ),
        ),
    )

    record_time = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    row = AccountSnapshotModel.from_domain(snapshot, recorded_at=record_time)

    assert row.net_value_usdc == Decimal("352.75")
    assert row.recorded_at == record_time
    assert row.balance_usdc == Decimal("250.00")
    # raw_payload 同步保留 net_value，方便审计回放。
    assert row.raw_payload["net_value_usdc"] == "352.75"


def test_account_snapshot_model_respects_explicit_net_value() -> None:
    snapshot = AccountSnapshot(balance_usdc=Decimal("100"))
    row = AccountSnapshotModel.from_domain(
        snapshot,
        net_value_usdc=Decimal("12345.67"),
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert row.net_value_usdc == Decimal("12345.67")


@pytest.mark.asyncio
async def test_equity_curve_clamps_invalid_windows_and_intervals() -> None:
    async def fake_query(*, since, until, interval_ms, account_key):
        return ()

    service = PortfolioHistoryService(query_history=fake_query)
    result = await service.equity_curve(window_ms=-5, interval_ms=0)
    payload = result.to_payload()
    assert payload["window_ms"] == DEFAULT_WINDOW_MS
    assert payload["interval_ms"] == DEFAULT_INTERVAL_MS
    assert payload["sample_size"] == 0
    assert payload["max_drawdown_pct"] == "0"
    assert payload["peak_at"] is None
    assert payload["trough_at"] is None
