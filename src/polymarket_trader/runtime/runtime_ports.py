"""Quant decision 与运行时基础设施的端口集合。

依赖注入容器，承载 quant decision 依赖的 2 个端口：

- ``lifecycle``：lifecycle event bus（LIVE_STATE_NO_FEASIBLE_SOURCE 等）
- ``metrics``：workflow bounded counter 上报

其余端口（market / orderbook / account / history / runtime / config /
telemetry / clock / parameter）已删除——workflow 代码直接读 store 即可，
无需 Protocol 中转。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from polymarket_trader.runtime.lifecycle_bus import LifecycleBus


class MetricsPort(Protocol):
    """workflow 层向 framework MetricsRegistry 上报 bounded counter 的端口。

    Label 值必须有界（如 ``reason`` 是 StrEnum 值）；不可传 condition_id /
    token_id 这类无界值——会让 metrics registry 无限膨胀。
    """

    def inc_counter(
        self,
        name: str,
        amount: float = 1.0,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimePorts:
    lifecycle: LifecycleBus | None = None
    metrics: MetricsPort | None = None
