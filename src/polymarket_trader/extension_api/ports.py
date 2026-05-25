"""Quant 策略与运行时基础设施的端口集合。

历史上是"框架 + 插件"二次开发框架的依赖注入容器，定义了 11 个可选端口。
现在策略和框架已合并，仅保留实际被消费的 4 个：

- ``parameter``：参数 override store（GET/PUT /parameters）
- ``lifecycle``：lifecycle event bus（LIVE_STATE_NO_FEASIBLE_SOURCE 等）
- ``metrics``：策略 bounded counter 上报
- ``season_state``：series winner pythagorean fallback 用赛季积分榜

其余端口（market / orderbook / account / history / runtime / config /
telemetry / clock）已删除——策略代码直接读 store 即可，无需 Protocol 中转。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from polymarket_trader.domain.sports_season import SeasonSnapshot
from polymarket_trader.extension_api.lifecycle import LifecycleBus


class MetricsPort(Protocol):
    """策略层向 framework MetricsRegistry 上报 bounded counter 的端口。

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


class ParameterPort(Protocol):
    """策略层访问运行时参数 override 的端口。

    用法：``ports.parameter.get('strategy', 'min_edge_bps', default=cfg.X)``。
    无 override 时返回 ``default``，所以策略代码默认行为不变。Override 的全
    集和写入入口由 ``GET/PUT /parameters`` 路由暴露——白名单约束在框架侧。
    """

    def get(self, scope: str, key: str, *, default: Any = None) -> Any: ...

    def has_override(self, scope: str, key: str) -> bool: ...

    def register_strategy_defaults(self, config: Any) -> None: ...


class SeasonStateReadPort(Protocol):
    """策略层访问赛季积分榜快照的端口（series winner 的 pythagorean fallback）。"""

    def season_snapshot(self) -> SeasonSnapshot: ...


@dataclass(frozen=True, slots=True)
class ExtensionPorts:
    lifecycle: LifecycleBus | None = None
    parameter: ParameterPort | None = None
    metrics: MetricsPort | None = None
    season_state: SeasonStateReadPort | None = None
