"""RuntimePorts 的具体实现绑定 framework 内的 store / bus。

只保留 2 个实际被 quant 消费的端口：lifecycle / metrics。
"""

from __future__ import annotations

from typing import Any, Mapping

from polymarket_trader.observability.metrics import MetricsRegistry
from polymarket_trader.runtime.lifecycle_bus import InProcessLifecycleBus
from polymarket_trader.runtime.runtime_ports import RuntimePorts


class MetricsRegistryMetricsPort:
    """把 framework MetricsRegistry 对外暴露成 workflow 可用的 bounded counter 端口。"""

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


def build_runtime_ports(
    *,
    lifecycle_bus: InProcessLifecycleBus | None = None,
    metrics_registry: MetricsRegistry | None = None,
) -> RuntimePorts:
    metrics_port = (
        MetricsRegistryMetricsPort(registry=metrics_registry)
        if metrics_registry is not None
        else NullMetricsPort()
    )
    return RuntimePorts(
        lifecycle=lifecycle_bus,
        metrics=metrics_port,
    )
