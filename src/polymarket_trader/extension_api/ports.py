from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.sports_season import SeasonSnapshot
from polymarket_trader.extension_api.context import AccountSnapshotView
from polymarket_trader.extension_api.lifecycle import LifecycleBus


class MarketReadPort(Protocol):
    def get_market(
        self,
        *,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> Market | None: ...

    def list_markets(self) -> tuple[Market, ...]: ...


class OrderbookReadPort(Protocol):
    def get_orderbook(self, token_id: str) -> OrderbookSnapshot | None: ...


class AccountReadPort(Protocol):
    def snapshot(self) -> AccountSnapshotView: ...


class HistoryReadPort(Protocol):
    def open_orders(self, *, condition_id: str, token_id: str) -> tuple[Order, ...]: ...


class RuntimeReadPort(Protocol):
    def is_market_paused(self, condition_id: str) -> bool: ...

    def can_open_new_entries(self) -> bool: ...


class ConfigReadPort(Protocol):
    def sanitized_framework_config(self) -> Mapping[str, Any]: ...

    def extension_config_metadata(self) -> Mapping[str, Any]: ...


class TelemetryPort(Protocol):
    def record_event(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> None: ...


class MetricsPort(Protocol):
    """策略层向 framework MetricsRegistry 同步上报有界 counter 的端口。

    与 ``TelemetryPort`` 的差别：``record_event`` 是审计事件（高维度、长 payload），
    ``MetricsPort.inc_counter`` 是 O(1) 内存计数（低维度、bounded label 集合）。
    P0 决策路径用这个上报命中/拒绝原因，主链路只产生 dict 写入开销，不做 IO。

    Label 值必须有界（如 ``sub_type`` ∈ {winner, total_games, ...}、``reason`` 是
    StrEnum 值）；不可传 condition_id / token_id 这类无界值，会让 metrics registry
    无限膨胀。
    """

    def inc_counter(
        self,
        name: str,
        amount: float = 1.0,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None: ...


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class ParameterPort(Protocol):
    """策略层访问运行时参数 override 的端口。

    用法：``ports.parameter.get('strategy', 'min_edge_bps', default=cfg.X)``。
    无 override 时返回 ``default``，所以策略代码默认行为不变。Override 的全
    集和写入入口由 ``GET/PUT /parameters`` 路由暴露——白名单约束在框架侧。
    """

    def get(self, scope: str, key: str, *, default: Any = None) -> Any: ...

    def has_override(self, scope: str, key: str) -> bool: ...

    def register_strategy_defaults(self, config: Any) -> None:
        """策略主动注册自己的配置对象，让 store 在 GET /parameters 返回 strategy.*
        参数的当前 default_value（按 spec 注册的 key 用 getattr 读对应字段）。
        """
        ...


class SeasonStateReadPort(Protocol):
    """策略层访问赛季积分榜 / 系列赛分快照的端口。

    用途：series winner 定价时做 Pythagorean win-pct fallback；无 game_odds API 时
    保证有兜底概率来源，不直接返回 MISSING_SERIES_ODDS。
    """

    def season_snapshot(self) -> SeasonSnapshot: ...


@dataclass(frozen=True, slots=True)
class ExtensionPorts:
    market: MarketReadPort | None = None
    orderbook: OrderbookReadPort | None = None
    account: AccountReadPort | None = None
    history: HistoryReadPort | None = None
    runtime: RuntimeReadPort | None = None
    config: ConfigReadPort | None = None
    telemetry: TelemetryPort | None = None
    clock: ClockPort | None = None
    lifecycle: LifecycleBus | None = None
    parameter: ParameterPort | None = None
    metrics: MetricsPort | None = None
    season_state: SeasonStateReadPort | None = None
