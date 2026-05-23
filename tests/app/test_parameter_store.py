"""``ParameterStore`` 行为 + ``GET/PUT/DELETE /parameters`` 路由。

覆盖：
- get_spec 白名单守门：未注册的 (scope, key) 抛 KeyError
- coerce 失败抛 ValueError；负数预算被拒
- set/get/clear 循环回到 default
- snapshot / registry_payload 形状
- event_bus.publish 在 set/clear 时被调用
- 路由层 404 / 422 / 200
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.routes.parameters import router as parameters_router
from polymarket_trader.app.parameter_store import ParameterStore, get_spec


class _SpyEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[Any, Any]] = []

    async def publish(self, priority: Any, event: Any) -> None:
        self.published.append((priority, event))


async def _set(store: ParameterStore, **kwargs: Any) -> Any:
    return await store.set(**kwargs)


def test_registry_contains_expected_specs() -> None:
    assert get_spec("settings", "portfolio_budget_usdc") is not None
    # Kelly sizing 替代了旧 max_order/market/total/open_orders 静态上限；白名单
    # 现在收 kelly_* 系列 + bankroll 相关参数。
    assert get_spec("settings", "kelly_fraction") is not None
    assert get_spec("settings", "kelly_max_position_fraction") is not None
    assert get_spec("settings", "kelly_min_edge") is not None
    assert get_spec("settings", "kelly_min_stake_usdc") is not None
    assert get_spec("strategy", "tail_outright_min_edge_bps") is not None
    assert get_spec("strategy", "tail_outright_min_profit_per_share") is not None
    assert get_spec("settings", "wallet_private_key") is None  # 不在白名单
    # 旧静态上限字段必须从白名单中移除——agent 不能再 PUT 这些已删除的 Settings 字段。
    assert get_spec("settings", "max_order_usdc") is None
    assert get_spec("settings", "max_market_usdc") is None
    assert get_spec("settings", "max_total_usdc") is None
    assert get_spec("settings", "max_open_orders") is None


def test_unknown_param_raises_key_error() -> None:
    store = ParameterStore()
    with pytest.raises(KeyError):
        asyncio.run(_set(store, scope="settings", key="bogus", value=1))


def test_coerce_negative_decimal_rejected() -> None:
    store = ParameterStore()
    # portfolio_budget_usdc 用 _coerce_decimal_non_negative，负数应抛 "non-negative"。
    with pytest.raises(ValueError, match="non-negative"):
        asyncio.run(_set(store, scope="settings", key="portfolio_budget_usdc", value="-5"))


def test_coerce_probability_above_one_rejected() -> None:
    store = ParameterStore()
    with pytest.raises(ValueError, match="0 and 1"):
        asyncio.run(_set(store, scope="strategy", key="entry_no_price_max", value="1.2"))


def test_set_then_get_returns_override() -> None:
    store = ParameterStore()
    asyncio.run(_set(store, scope="settings", key="portfolio_budget_usdc", value="50"))
    assert store.get("settings", "portfolio_budget_usdc") == Decimal("50")
    assert store.has_override("settings", "portfolio_budget_usdc") is True


def test_clear_falls_back_to_default() -> None:
    store = ParameterStore()
    asyncio.run(_set(store, scope="settings", key="portfolio_budget_usdc", value="50"))
    asyncio.run(store.clear(scope="settings", key="portfolio_budget_usdc"))
    assert store.has_override("settings", "portfolio_budget_usdc") is False
    assert (
        store.get("settings", "portfolio_budget_usdc", default=Decimal("10"))
        == Decimal("10")
    )


def test_event_bus_receives_override_event() -> None:
    bus = _SpyEventBus()
    store = ParameterStore(event_bus=bus)
    asyncio.run(
        _set(
            store,
            scope="strategy",
            key="tail_outright_min_edge_bps",
            value=500,
            operator="agent",
        )
    )
    assert len(bus.published) == 1
    _, event = bus.published[0]
    assert event.event_type.value == "parameter_override_applied"
    assert event.payload["scope"] == "strategy"
    assert event.payload["key"] == "tail_outright_min_edge_bps"
    # int 类型不通过 _stringify 转字符串；Decimal 才会。
    assert event.payload["new_value"] == 500


def test_registry_payload_includes_override_state() -> None:
    store = ParameterStore()
    # 用一个真实存在的 kelly_* 字段验证 override 显示——用 kelly_min_edge 因为
    # 它的 coerce 容忍 0-1 之间任意 Decimal，不会被语义校验额外拒。
    asyncio.run(_set(store, scope="settings", key="kelly_min_edge", value="0.05"))
    items = store.registry_payload()
    by_key = {(item["scope"], item["key"]): item for item in items}
    target = by_key[("settings", "kelly_min_edge")]
    assert target["override"] is not None
    assert target["override"]["value"] == "0.05"
    untouched = by_key[("settings", "portfolio_budget_usdc")]
    assert untouched["override"] is None


# --------------------------- HTTP route ---------------------------------------


class _FakeRuntime:
    def __init__(self, store: ParameterStore | None) -> None:
        self.parameter_store = store


@pytest.fixture()
def http_client() -> tuple[TestClient, ParameterStore]:
    store = ParameterStore()
    app = FastAPI()
    app.include_router(parameters_router)
    app.state.runtime = _FakeRuntime(store)
    app.state.get_runtime = lambda: app.state.runtime
    return TestClient(app), store


def test_route_set_then_list_overrides(http_client: tuple[TestClient, ParameterStore]) -> None:
    client, _ = http_client
    response = client.put(
        "/parameters/settings/portfolio_budget_usdc",
        json={"value": "25", "operator": "test"},
    )
    assert response.status_code == 200
    assert response.json()["value"] == "25"

    response = client.get("/parameters/overrides")
    assert response.status_code == 200
    overrides = response.json()["overrides"]
    assert len(overrides) == 1
    assert overrides[0]["key"] == "portfolio_budget_usdc"


def test_route_unknown_param_returns_404(http_client: tuple[TestClient, ParameterStore]) -> None:
    client, _ = http_client
    response = client.put(
        "/parameters/settings/bogus",
        json={"value": "1", "operator": "test"},
    )
    assert response.status_code == 404
    assert "unknown parameter" in response.json()["detail"]


def test_route_invalid_value_returns_422(http_client: tuple[TestClient, ParameterStore]) -> None:
    client, _ = http_client
    response = client.put(
        "/parameters/settings/portfolio_budget_usdc",
        json={"value": "-1", "operator": "test"},
    )
    assert response.status_code == 422


def test_route_delete_clears_override(http_client: tuple[TestClient, ParameterStore]) -> None:
    client, store = http_client
    client.put("/parameters/settings/portfolio_budget_usdc", json={"value": "25"})
    response = client.delete("/parameters/settings/portfolio_budget_usdc")
    assert response.status_code == 200
    assert response.json()["cleared"] is True
    assert store.has_override("settings", "portfolio_budget_usdc") is False


def test_route_503_when_store_missing() -> None:
    app = FastAPI()
    app.include_router(parameters_router)
    app.state.runtime = _FakeRuntime(None)
    app.state.get_runtime = lambda: app.state.runtime
    client = TestClient(app)
    response = client.get("/parameters")
    assert response.status_code == 503
