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
