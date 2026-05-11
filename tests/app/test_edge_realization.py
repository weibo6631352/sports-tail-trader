"""``build_edge_realization`` 数学正确性 + 分桶 + ``GET /analytics/edge-realization`` 路由。

覆盖：
- ``predicted_edge_bps = (fair - entry) / fair * 10000`` 公式
- ``actual_return_bps`` 用 ``realized_pnl/cost`` 优先（已平仓），否则 ``cash_pnl/cost``
- cost = 0 时 actual_return_bps = None（避免除零）
- ``position_status`` open/redeemable/settled_zero/missing 四态
- 预测 edge 分桶边界（50/100/200/500/1000bps）
- 胜率 = 正回报样本占比
- 路由层把 ``since`` / ``until`` 转 TimeRange 并透传
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.analytics import router as analytics_router
from polymarket_trader.app.edge_realization import (
    aggregate_by_predicted_edge_buckets,
    build_edge_realization,
)
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.position import Position


BASE = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


def _decision(
    *,
    record_id: str,
    condition_id: str,
    token_id: str | None,
    fair_value: str | None,
    entry_price: str | None,
) -> DecisionRecord:
    output: dict[str, Any] = {}
    if fair_value is not None:
        output["fair_value"] = fair_value
    if entry_price is not None:
        output["entry_price_cap"] = entry_price
    return DecisionRecord(
        strategy_id="sports_tail",
        record_id=record_id,
        trace_id=f"trace-{record_id}",
        condition_id=condition_id,
        token_id=token_id,
        decision_input={},
        decision_output=output,
        accepted=True,
        reason=None,
        created_at=BASE,
    )


def _position(
    *,
    condition_id: str,
    token_id: str,
    cost: str | None,
    cash: str | None = None,
    realized: str | None = None,
    redeemable: bool | None = False,
    current_value: str = "0",
) -> Position:
    return Position(
        strategy_id="sports_tail",
        condition_id=condition_id,
        token_id=token_id,
        shares=Decimal("0"),
        cost_usdc=Decimal(cost) if cost is not None else Decimal("0"),
        cash_pnl=None if cash is None else Decimal(cash),
        realized_pnl=None if realized is None else Decimal(realized),
        redeemable=redeemable,
        current_value=Decimal(current_value),
    )


def test_predicted_edge_bps_formula() -> None:
    # fair=0.50, entry=0.45 → edge = (0.50-0.45)/0.50 = 0.10 → 1000bps
    decisions = (
        _decision(
            record_id="r1",
            condition_id="c1",
            token_id="t1",
            fair_value="0.50",
            entry_price="0.45",
        ),
    )
    items = build_edge_realization(decisions=decisions, positions_by_key={})
    assert items[0].predicted_edge_bps == Decimal("1000")
    assert items[0].position_status == "missing"
    assert items[0].actual_return_bps is None


def test_actual_return_uses_realized_first() -> None:
    decisions = (
        _decision(
            record_id="r1",
            condition_id="c1",
            token_id="t1",
            fair_value="0.50",
            entry_price="0.45",
        ),
    )
    position = _position(
        condition_id="c1",
        token_id="t1",
        cost="10",
        cash="3",
        realized="5",  # 优先 → 50% → 5000bps
    )
    items = build_edge_realization(decisions=decisions, positions_by_key={("c1", "t1"): position})
    assert items[0].actual_return_bps == Decimal("5000")


def test_actual_return_falls_back_to_cash_when_no_realized() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair_value="0.50", entry_price="0.45"),
    )
    position = _position(condition_id="c1", token_id="t1", cost="10", cash="2", realized=None)
    items = build_edge_realization(decisions=decisions, positions_by_key={("c1", "t1"): position})
    # 2/10 = 20% = 2000bps
    assert items[0].actual_return_bps == Decimal("2000")
    assert items[0].position_status == "open"


def test_actual_return_none_when_cost_zero() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair_value="0.50", entry_price="0.45"),
    )
    position = _position(condition_id="c1", token_id="t1", cost="0", cash="2")
    items = build_edge_realization(decisions=decisions, positions_by_key={("c1", "t1"): position})
    assert items[0].actual_return_bps is None


def test_position_status_settled_zero() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair_value="0.50", entry_price="0.45"),
    )
    position = _position(
        condition_id="c1",
        token_id="t1",
        cost="10",
        cash="-10",  # 完全输掉
        redeemable=True,
        current_value="0",
    )
    items = build_edge_realization(decisions=decisions, positions_by_key={("c1", "t1"): position})
    assert items[0].position_status == "settled_zero"


def test_missing_fair_or_entry_yields_no_prediction() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair_value=None, entry_price="0.45"),
        _decision(record_id="r2", condition_id="c2", token_id="t2", fair_value="0.50", entry_price=None),
    )
    items = build_edge_realization(decisions=decisions, positions_by_key={})
    for item in items:
        assert item.predicted_edge_bps is None


def test_bucket_boundaries() -> None:
    decisions = (
        # 30bps → 0-50
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair_value="1.00", entry_price="0.997"),
        # 100bps → 100-200 边界归入更高桶（< 100 落 50-100）
        _decision(record_id="r2", condition_id="c2", token_id="t2", fair_value="1.00", entry_price="0.99"),
        # 600bps → 500-1000
        _decision(record_id="r3", condition_id="c3", token_id="t3", fair_value="1.00", entry_price="0.94"),
        # 1500bps → >1000
        _decision(record_id="r4", condition_id="c4", token_id="t4", fair_value="1.00", entry_price="0.85"),
    )
    items = build_edge_realization(decisions=decisions, positions_by_key={})
    buckets = aggregate_by_predicted_edge_buckets(items)
    bucket_counts = {b["bucket"]: b["count"] for b in buckets}
    assert bucket_counts["0-50bps"] == 1
    assert bucket_counts["100-200bps"] == 1  # 100 边界归入 100-200 桶
    assert bucket_counts["500-1000bps"] == 1
    assert bucket_counts[">1000bps"] == 1


def test_win_rate_and_mean() -> None:
    decisions = tuple(
        _decision(
            record_id=f"r{i}",
            condition_id=f"c{i}",
            token_id=f"t{i}",
            fair_value="1.00",
            entry_price="0.95",  # 500bps 预测 → 500-1000 桶
        )
        for i in range(3)
    )
    positions = {
        ("c0", "t0"): _position(condition_id="c0", token_id="t0", cost="10", realized="5"),  # +5000
        ("c1", "t1"): _position(condition_id="c1", token_id="t1", cost="10", realized="-2"),  # -2000
        ("c2", "t2"): _position(condition_id="c2", token_id="t2", cost="10", realized="3"),  # +3000
    }
    items = build_edge_realization(decisions=decisions, positions_by_key=positions)
    buckets = aggregate_by_predicted_edge_buckets(items)
    target = next(b for b in buckets if b["bucket"] == "500-1000bps")
    assert target["count"] == 3
    assert target["with_return_count"] == 3
    # mean = (5000 - 2000 + 3000)/3 = 2000
    assert Decimal(target["mean_actual_return_bps"]) == Decimal("2000")
    # win = 2/3
    assert Decimal(target["win_rate"]) == Decimal("2") / Decimal("3")


# --------------------------- /analytics/edge-realization -----------------------


class _RecordingService:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def edge_realization_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {"items": [], "buckets": [], "limit": kwargs.get("limit", 200)}


@pytest.fixture()
def client() -> tuple[TestClient, _RecordingService]:
    service = _RecordingService()
    app = FastAPI()
    app.include_router(analytics_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return TestClient(app), service


def test_edge_realization_route_passes_filters(
    client: tuple[TestClient, _RecordingService],
) -> None:
    test_client, service = client
    response = test_client.get(
        "/analytics/edge-realization",
        params={
            "limit": 50,
            "strategy_id": "sports_tail",
            "condition_id": "cond-X",
            "since": 100,
            "until": 500,
        },
    )
    assert response.status_code == 200
    kwargs = service.kwargs
    assert kwargs is not None
    assert kwargs["limit"] == 50
    assert kwargs["strategy_id"] == "sports_tail"
    assert kwargs["condition_id"] == "cond-X"
    tr = kwargs["time_range"]
    assert tr is not None and (tr.since_ms, tr.until_ms) == (100, 500)
