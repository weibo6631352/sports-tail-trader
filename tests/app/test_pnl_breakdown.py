"""``build_pnl_breakdown`` 聚合行为 + ``GET /portfolio/pnl-breakdown`` 路由。

覆盖：
- 按 ``strategy_id`` / ``market_slug`` / ``condition_id`` / ``category`` /
  ``outcome`` / ``redeemable_status`` 分组的 PnL 累加正确性
- 缺失 ``realized_pnl`` 视为 0，不丢弃仓位
- 排序按 ``realized_pnl`` 降序
- ``totals`` 合计字段
- 非法 ``group_by`` 返回 422
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.portfolio import router as portfolio_router
from polymarket_trader.app.pnl_breakdown import (
    build_pnl_breakdown,
    aggregate_totals,
    valid_group_by_values,
)
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.position import Position


def _market(*, condition_id: str, category: str, outcomes: tuple[MarketOutcome, ...]) -> Market:
    return Market(
        condition_id=condition_id,
        market_slug=f"slug-{condition_id}",
        outcomes=outcomes,
        category=category,
        trading_status=TradingStatus.ELIGIBLE,
    )


def _position(
    *,
    strategy_id: str,
    condition_id: str,
    token_id: str,
    market_slug: str | None = None,
    realized: Decimal | None = Decimal("0"),
    cash: Decimal | None = Decimal("0"),
    current: Decimal | None = Decimal("0"),
    cost: Decimal | None = Decimal("0"),
    redeemable: bool | None = False,
) -> Position:
    return Position(
        strategy_id=strategy_id,
        condition_id=condition_id,
        token_id=token_id,
        market_slug=market_slug,
        shares=Decimal("0"),
        cost_usdc=cost or Decimal("0"),
        realized_pnl=realized,
        cash_pnl=cash,
        current_value=current,
        redeemable=redeemable,
    )


def test_group_by_strategy_id_sums_correctly() -> None:
    positions = (
        _position(
            strategy_id="alpha",
            condition_id="c1",
            token_id="t1",
            realized=Decimal("10"),
            cash=Decimal("3"),
        ),
        _position(
            strategy_id="alpha",
            condition_id="c2",
            token_id="t2",
            realized=Decimal("-2"),
            cash=Decimal("1"),
        ),
        _position(
            strategy_id="beta",
            condition_id="c3",
            token_id="t3",
            realized=Decimal("5"),
            cash=Decimal("0"),
        ),
    )
    rows = build_pnl_breakdown(positions=positions, markets_by_condition={}, group_by="strategy_id")
    assert [r.group_key for r in rows] == ["alpha", "beta"]  # alpha 8 > beta 5
    assert rows[0].position_count == 2
    assert rows[0].total_realized_pnl_usdc == Decimal("8")
    assert rows[1].total_realized_pnl_usdc == Decimal("5")


def test_group_by_none_realized_treated_as_zero() -> None:
    positions = (
        _position(strategy_id="alpha", condition_id="c1", token_id="t1", realized=None, cash=None),
        _position(strategy_id="alpha", condition_id="c2", token_id="t2", realized=Decimal("3")),
    )
    rows = build_pnl_breakdown(positions=positions, markets_by_condition={}, group_by="strategy_id")
    assert len(rows) == 1
    assert rows[0].position_count == 2  # 缺失 PnL 的也算入
    assert rows[0].total_realized_pnl_usdc == Decimal("3")


def test_group_by_category_uses_market_join() -> None:
    markets = {
        "c1": _market(
            condition_id="c1",
            category="basketball",
            outcomes=(MarketOutcome(token_id="t1", outcome="Yes"),),
        ),
        "c2": _market(
            condition_id="c2",
            category="baseball",
            outcomes=(MarketOutcome(token_id="t2", outcome="No"),),
        ),
    }
    positions = (
        _position(strategy_id="alpha", condition_id="c1", token_id="t1", realized=Decimal("10")),
        _position(strategy_id="alpha", condition_id="c2", token_id="t2", realized=Decimal("-5")),
    )
    rows = build_pnl_breakdown(positions=positions, markets_by_condition=markets, group_by="category")
    assert [r.group_key for r in rows] == ["basketball", "baseball"]


def test_group_by_outcome_resolves_token() -> None:
    markets = {
        "c1": _market(
            condition_id="c1",
            category="basketball",
            outcomes=(
                MarketOutcome(token_id="yes-tok", outcome="Yes"),
                MarketOutcome(token_id="no-tok", outcome="No"),
            ),
        ),
    }
    positions = (
        _position(strategy_id="alpha", condition_id="c1", token_id="yes-tok", realized=Decimal("3")),
        _position(strategy_id="alpha", condition_id="c1", token_id="no-tok", realized=Decimal("1")),
    )
    rows = build_pnl_breakdown(positions=positions, markets_by_condition=markets, group_by="outcome")
    assert {r.group_key for r in rows} == {"Yes", "No"}


def test_group_by_redeemable_status() -> None:
    positions = (
        _position(strategy_id="alpha", condition_id="c1", token_id="t1", redeemable=True, realized=Decimal("4")),
        _position(strategy_id="alpha", condition_id="c2", token_id="t2", redeemable=False, realized=Decimal("3")),
        _position(strategy_id="alpha", condition_id="c3", token_id="t3", redeemable=None, realized=Decimal("2")),
    )
    rows = build_pnl_breakdown(positions=positions, markets_by_condition={}, group_by="redeemable_status")
    assert {r.group_key for r in rows} == {"redeemable", "open", "unknown"}


def test_aggregate_totals_sums_across_rows() -> None:
    positions = (
        _position(
            strategy_id="alpha",
            condition_id="c1",
            token_id="t1",
            realized=Decimal("10"),
            cash=Decimal("2"),
            current=Decimal("5"),
            cost=Decimal("3"),
        ),
        _position(
            strategy_id="beta",
            condition_id="c2",
            token_id="t2",
            realized=Decimal("-3"),
            cash=Decimal("-1"),
            current=Decimal("0"),
            cost=Decimal("1"),
        ),
    )
    rows = build_pnl_breakdown(positions=positions, markets_by_condition={}, group_by="strategy_id")
    totals = aggregate_totals(rows)
    assert totals["position_count"] == 2
    assert totals["realized_pnl_usdc"] == "7"
    assert totals["cost_usdc"] == "4"


def test_invalid_group_by_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported group_by"):
        build_pnl_breakdown(positions=(), markets_by_condition={}, group_by="bogus")


def test_valid_group_by_values_returns_sorted_tuple() -> None:
    vals = valid_group_by_values()
    assert "strategy_id" in vals
    assert "category" in vals
    assert vals == tuple(sorted(vals))


# --------------------------- /portfolio/pnl-breakdown --------------------------


class _RecordingService:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def pnl_breakdown_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        if kwargs.get("group_by") == "bogus":
            raise ValueError("unsupported group_by: bogus")
        return {"group_by": kwargs["group_by"], "rows": [], "totals": {}}


@pytest.fixture()
def client() -> tuple[TestClient, _RecordingService]:
    service = _RecordingService()
    app = FastAPI()
    app.include_router(portfolio_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return TestClient(app), service


def test_pnl_breakdown_route_default(client: tuple[TestClient, _RecordingService]) -> None:
    test_client, service = client
    response = test_client.get("/portfolio/pnl-breakdown")
    assert response.status_code == 200
    assert service.kwargs is not None
    assert service.kwargs["group_by"] == "strategy_id"


def test_pnl_breakdown_route_invalid_returns_422(client: tuple[TestClient, _RecordingService]) -> None:
    test_client, _ = client
    response = test_client.get("/portfolio/pnl-breakdown", params={"group_by": "bogus"})
    assert response.status_code == 422


def test_pnl_breakdown_route_passes_filters(client: tuple[TestClient, _RecordingService]) -> None:
    test_client, service = client
    response = test_client.get(
        "/portfolio/pnl-breakdown",
        params={
            "group_by": "category",
            "strategy_id": "sports_tail",
            "condition_id": "cond-X",
            "position_limit": 1000,
        },
    )
    assert response.status_code == 200
    kwargs = service.kwargs
    assert kwargs is not None
    assert kwargs["group_by"] == "category"
    assert kwargs["strategy_id"] == "sports_tail"
    assert kwargs["condition_id"] == "cond-X"
    assert kwargs["position_limit"] == 1000
