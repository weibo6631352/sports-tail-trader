from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.sports_season import SeasonSnapshot
from polymarket_trader.observability.metrics import MetricsRegistry
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.runtime.lifecycle_bus import InProcessLifecycleBus
from polymarket_trader.extension_api import ExtensionPorts

OrderbookReader = Callable[[str], OrderbookSnapshot | None]
AccountSnapshotProvider = Callable[[], AccountSnapshot]


class MarketDataPort:
    def __init__(
        self,
        *,
        registry: MarketRegistry,
        orderbook_reader: OrderbookReader | None = None,
    ) -> None:
        self._registry = registry
        self._orderbook_reader = orderbook_reader

    def bind_orderbook_reader(self, orderbook_reader: OrderbookReader | None) -> None:
        self._orderbook_reader = orderbook_reader

    def get_market(
        self,
        *,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> Market | None:
        if condition_id is not None:
            market = self._registry.get_by_condition_id(condition_id)
            if market is not None:
                return market
        if market_slug is not None:
            market = self._registry.get_by_slug(market_slug)
            if market is not None:
                return market
        if token_id is not None:
            return self._registry.get_by_token_id(token_id)
        return None

    def list_markets(self) -> tuple[Market, ...]:
        return self._registry.snapshot().markets

    def get_orderbook(self, token_id: str) -> OrderbookSnapshot | None:
        if self._orderbook_reader is None:
            return None
        return self._orderbook_reader(token_id)


class AccountStatePort:
    def __init__(self, *, snapshot_provider: AccountSnapshotProvider) -> None:
        self._snapshot_provider = snapshot_provider

    def snapshot(self) -> AccountSnapshot:
        return self._snapshot_provider()


class RegistryStatePort:
    def __init__(self, *, registry: MarketRegistry) -> None:
        self._registry = registry

    def get_market(
        self,
        *,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> Market | None:
        return MarketDataPort(registry=self._registry).get_market(
            condition_id=condition_id,
            token_id=token_id,
            market_slug=market_slug,
        )

    def list_markets(self) -> tuple[Market, ...]:
        return self._registry.snapshot().markets


class RuntimeStatePort:
    def __init__(self, *, snapshot_provider: AccountSnapshotProvider) -> None:
        self._snapshot_provider = snapshot_provider

    def is_market_paused(self, condition_id: str) -> bool:
        return self._snapshot_provider().is_market_paused(condition_id)

    def can_open_new_entries(self) -> bool:
        return self._snapshot_provider().allow_new_entries


class OrderHistoryPort:
    def __init__(self, *, snapshot_provider: AccountSnapshotProvider) -> None:
        self._snapshot_provider = snapshot_provider

    def open_orders(self, *, condition_id: str, token_id: str) -> tuple[Order, ...]:
        return self._snapshot_provider().open_orders_for_market(condition_id, token_id)


class NullTelemetryPort:
    def record_event(
        self,
        name: str,
        *,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        return None


class NullMetricsPort:
    """``MetricsPort`` 的空实现——测试 / 不接 metrics 注册表时使用。

    与 ``MetricsRegistryMetricsPort`` 接口完全一致；策略代码透明地调用
    ``ports.metrics.inc_counter(...)``，不必感知 registry 是否绑定。
    """

    def inc_counter(
        self,
        name: str,
        amount: float = 1.0,
        *,
        labels: Mapping[str, object] | None = None,
    ) -> None:
        return None


class MetricsRegistryMetricsPort:
    """把 framework ``MetricsRegistry`` 暴露给策略层的 ``MetricsPort``。

    只暴露 ``inc_counter``（同步 O(1) 内存写入），让 P0 决策路径可以在不阻塞
    主链路的情况下记录命中/拒绝原因。Gauges / Histograms 不开放给策略——保留
    在 framework 侧避免策略意外引入无界 label。
    """

    def __init__(self, *, registry: MetricsRegistry) -> None:
        self._registry = registry

    def inc_counter(
        self,
        name: str,
        amount: float = 1.0,
        *,
        labels: Mapping[str, object] | None = None,
    ) -> None:
        self._registry.inc_counter(name, amount, labels=labels)


class UtcClockPort:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ParameterStorePort:
    """``ParameterPort`` 的默认实现——委托给 ``ParameterStore``。

    策略层只接触 ``ports.parameter.get(scope, key, default=)`` 这个轻接口，
    不直接依赖 ``ParameterStore`` 的内部状态（白名单、审计 event 等）。
    """

    def __init__(self, *, store: Any) -> None:
        self._store = store

    def get(self, scope: str, key: str, *, default: Any = None) -> Any:
        return self._store.get(scope, key, default=default)

    def has_override(self, scope: str, key: str) -> bool:
        return self._store.has_override(scope, key)

    def register_strategy_defaults(self, config: Any) -> None:
        """让策略把自己的配置对象注册到 store，用于 GET /parameters 返回 default_value。

        框架不直接读策略私有属性——由策略主动暴露 config 对象，存到 store 里。
        store._get_default_value 通过 ``getattr(config, key)`` 按 spec 注册的 key 读基准值。
        """
        self._store.bind_strategy_config(config)


def build_extension_ports(
    *,
    registry: MarketRegistry,
    snapshot_provider: AccountSnapshotProvider,
    orderbook_reader: OrderbookReader | None = None,
    lifecycle_bus: InProcessLifecycleBus | None = None,
    parameter_store: Any | None = None,
    metrics_registry: MetricsRegistry | None = None,
) -> ExtensionPorts:
    market_port = MarketDataPort(registry=registry, orderbook_reader=orderbook_reader)
    metrics_port = (
        MetricsRegistryMetricsPort(registry=metrics_registry)
        if metrics_registry is not None
        else NullMetricsPort()
    )
    return ExtensionPorts(
        market=market_port,
        orderbook=market_port,
        account=AccountStatePort(snapshot_provider=snapshot_provider),
        runtime=RuntimeStatePort(snapshot_provider=snapshot_provider),
        history=OrderHistoryPort(snapshot_provider=snapshot_provider),
        telemetry=NullTelemetryPort(),
        clock=UtcClockPort(),
        lifecycle=lifecycle_bus,
        parameter=ParameterStorePort(store=parameter_store) if parameter_store is not None else None,
        metrics=metrics_port,
        season_state=SeasonStatePort(),
    )


class SeasonStatePort:
    """把 ``SeasonStateStore`` 暴露给策略层。

    与 ``MarketDataPort.bind_orderbook_reader`` 同模式：先在 ``build_extension_ports``
    时注入空端口，等 ``season_state_store`` 在 ``build_runtime`` 后期创建完毕后
    调用 ``bind_store`` 填入。策略调用 ``season_snapshot()`` 返回积分榜快照，
    无 store 时返回空 snapshot，保证 pythagorean fallback 仍优雅降级。
    """

    def __init__(self) -> None:
        self._store: Any | None = None

    def bind_store(self, store: Any | None) -> None:
        self._store = store

    def season_snapshot(self) -> SeasonSnapshot:
        if self._store is None:
            return SeasonSnapshot(observed_at=datetime.now(timezone.utc))
        standings = tuple(self._store.all_standings())
        return SeasonSnapshot(
            observed_at=datetime.now(timezone.utc),
            standings=standings,
        )


def bind_extension_season_state(
    ports: ExtensionPorts,
    season_state_store: Any | None,
) -> None:
    """把 season_state_store 注入 ports.season_state（原地修改内部对象，不替换 ports）。"""
    season_port = ports.season_state
    if isinstance(season_port, SeasonStatePort):
        season_port.bind_store(season_state_store)


def bind_extension_orderbook_reader(
    ports: ExtensionPorts,
    orderbook_reader: OrderbookReader | None,
) -> None:
    market_port = ports.market
    if isinstance(market_port, MarketDataPort):
        market_port.bind_orderbook_reader(orderbook_reader)
