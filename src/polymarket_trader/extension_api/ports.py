from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
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
