"""AdminRuntimeQueryMixin 直接 unit 测试。

子类化 mixin + 注入 host helper stub，验证：
- 五个 runtime 投影方法都直通到 ``_runtime_view()`` 对应方法
- ``portfolio_snapshot`` 在缺少 DB session factory 时跳过 allocations 查询
- ``portfolio_snapshot`` 在有 DB 时拉 allocations 并经 serializer 投影
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from polymarket_trader.app.admin_query.runtime import AdminRuntimeQueryMixin
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.runtime.registry import MarketRegistrySnapshot


@dataclass
class _FakeRuntimeView:
    health: dict[str, Any] = field(default_factory=lambda: {"phase": "ok"})
    readiness: dict[str, Any] = field(default_factory=lambda: {"ready_to_trade": True})
    runtime: dict[str, Any] = field(default_factory=lambda: {"phase": "TRADING_ENABLED"})
    workers: dict[str, Any] = field(default_factory=lambda: {"discovery": "running"})
    metrics: dict[str, Any] = field(default_factory=lambda: {"counters": {}})

    def health_snapshot(self) -> dict[str, Any]:
        return self.health

    def readiness_snapshot(self) -> dict[str, Any]:
        return self.readiness

    async def runtime_snapshot(self) -> dict[str, Any]:
        return self.runtime

    def workers_snapshot(self) -> dict[str, Any]:
        return self.workers

    def metrics_snapshot(self) -> dict[str, Any]:
        return self.metrics


@dataclass
class _FakeSerializer:
    def allocation(self, allocation: Any) -> dict[str, Any]:
        return {"allocation_marker": getattr(allocation, "marker", None)}


@dataclass
class _FakeAllocation:
    marker: str


class _Host(AdminRuntimeQueryMixin):
    def __init__(
        self,
        *,
        account: AccountSnapshot,
        registry: MarketRegistrySnapshot,
        has_db: bool = False,
        allocations: tuple[Any, ...] = (),
    ) -> None:
        self._account = account
        self._registry = registry
        self._has_db = has_db
        self._allocations = allocations
        self._view = _FakeRuntimeView()
        self._ser = _FakeSerializer()

    def _runtime_view(self) -> _FakeRuntimeView:
        return self._view

    def _account_snapshot(self) -> AccountSnapshot:
        return self._account

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        return self._registry

    def _has_db_session_factory(self) -> bool:
        return self._has_db

    def _serializer(self) -> _FakeSerializer:
        return self._ser

    async def _with_repositories(self, callback: Callable[[Any], Any]) -> Any:
        return RepositoryPage(items=self._allocations, total=len(self._allocations), limit=50, offset=0)


def _empty_registry() -> MarketRegistrySnapshot:
    return MarketRegistrySnapshot(markets=())


def _empty_account() -> AccountSnapshot:
    return AccountSnapshot(
        balance_usdc=Decimal("100"),
        allowance_usdc=Decimal("200"),
        user_ws_connected=True,
        allow_new_entries=True,
        last_reconcile_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )


def test_health_readiness_workers_metrics_delegate_to_runtime_view() -> None:
    host = _Host(account=_empty_account(), registry=_empty_registry())
    assert host.health_snapshot() == {"phase": "ok"}
    assert host.readiness_snapshot() == {"ready_to_trade": True}
    assert host.workers_snapshot() == {"discovery": "running"}
    assert host.metrics_snapshot() == {"counters": {}}


def test_runtime_snapshot_is_awaitable_and_delegates() -> None:
    host = _Host(account=_empty_account(), registry=_empty_registry())
    payload = asyncio.run(host.runtime_snapshot())
    assert payload == {"phase": "TRADING_ENABLED"}


def test_portfolio_snapshot_without_db_returns_account_fields_and_empty_allocations() -> None:
    host = _Host(account=_empty_account(), registry=_empty_registry(), has_db=False)
    payload = asyncio.run(host.portfolio_snapshot())
    assert payload["balance_usdc"] == "100"
    assert payload["allowance_usdc"] == "200"
    assert payload["recent_allocations"] == []
    assert payload["markets_tracked"] == 0
    assert payload["allow_new_entries"] is True


def test_portfolio_snapshot_with_db_returns_serialized_allocations() -> None:
    allocations = (_FakeAllocation(marker="a1"), _FakeAllocation(marker="a2"))
    host = _Host(
        account=_empty_account(),
        registry=_empty_registry(),
        has_db=True,
        allocations=allocations,
    )
    payload = asyncio.run(host.portfolio_snapshot())
    assert payload["recent_allocations"] == [
        {"allocation_marker": "a1"},
        {"allocation_marker": "a2"},
    ]
    assert payload["position_count"] == 0


def test_portfolio_snapshot_serializes_decimals_and_iso_timestamp() -> None:
    host = _Host(account=_empty_account(), registry=_empty_registry(), has_db=False)
    payload = asyncio.run(host.portfolio_snapshot())
    # decimal_text 保留无尾零字符串；ISO 时间戳必须含 UTC 偏移
    assert isinstance(payload["balance_usdc"], str)
    assert payload["last_reconcile_at"].startswith("2026-05-11")
