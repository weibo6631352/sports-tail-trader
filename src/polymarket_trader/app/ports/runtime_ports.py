"""RuntimePorts 的具体实现绑定 framework 内的 store / bus。

历史上承载 11 个 port 的适配器；现在只保留 4 个实际被 quant 消费的：
parameter / lifecycle / metrics / season_state。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from polymarket_trader.domain.sports_season import SeasonSnapshot
from polymarket_trader.observability.metrics import MetricsRegistry
from polymarket_trader.runtime.lifecycle_bus import InProcessLifecycleBus
from polymarket_trader.runtime.runtime_ports import RuntimePorts


class MetricsRegistryMetricsPort:
    """把 framework MetricsRegistry 对外暴露成策略可用的 bounded counter 端口。"""

    def __init__(self, *, registry: MetricsRegistry) -> None:
        self._registry = registry

    def inc_counter(
        self,
        name: str,
        amount: float = 1.0,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        normalized = self._normalize_labels(labels)
        self._registry.increment(name, value=amount, labels=normalized)

    @staticmethod
    def _normalize_labels(labels: Mapping[str, Any] | None) -> dict[str, str]:
        if not labels:
            return {}
        return {str(k): str(v) for k, v in labels.items()}


class NullMetricsPort:
    """Metrics 注入未提供时的 no-op 占位。"""

    def inc_counter(
        self,
        name: str,
        amount: float = 1.0,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        del name, amount, labels


class ParameterStorePort:
    """适配 ParameterStore 给策略层用的 .get/.has_override 接口。"""

    def __init__(self, *, store: Any) -> None:
        self._store = store

    def get(self, scope: str, key: str, *, default: Any = None) -> Any:
        return self._store.get(scope, key, default=default)

    def has_override(self, scope: str, key: str) -> bool:
        return self._store.has_override(scope, key)

    def register_strategy_defaults(self, config: Any) -> None:
        self._store.bind_strategy_config(config)


class SeasonStatePort:
    """把 ``SeasonStateStore`` 暴露给策略层。"""

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


def build_extension_ports(
    *,
    lifecycle_bus: InProcessLifecycleBus | None = None,
    parameter_store: Any | None = None,
    metrics_registry: MetricsRegistry | None = None,
) -> RuntimePorts:
    metrics_port = (
        MetricsRegistryMetricsPort(registry=metrics_registry)
        if metrics_registry is not None
        else NullMetricsPort()
    )
    return RuntimePorts(
        lifecycle=lifecycle_bus,
        parameter=ParameterStorePort(store=parameter_store) if parameter_store is not None else None,
        metrics=metrics_port,
        season_state=SeasonStatePort(),
    )


def bind_extension_season_state(
    ports: RuntimePorts,
    season_state_store: Any | None,
) -> None:
    """把 season_state_store 注入 ports.season_state（原地修改）。"""
    season_port = ports.season_state
    if isinstance(season_port, SeasonStatePort):
        season_port.bind_store(season_state_store)
