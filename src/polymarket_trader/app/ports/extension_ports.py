from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.account import AccountSnapshot
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


class UtcClockPort:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def build_extension_ports(
    *,
    registry: MarketRegistry,
    snapshot_provider: AccountSnapshotProvider,
    orderbook_reader: OrderbookReader | None = None,
    lifecycle_bus: InProcessLifecycleBus | None = None,
) -> ExtensionPorts:
    market_port = MarketDataPort(registry=registry, orderbook_reader=orderbook_reader)
    return ExtensionPorts(
        market=market_port,
        orderbook=market_port,
        account=AccountStatePort(snapshot_provider=snapshot_provider),
        runtime=RuntimeStatePort(snapshot_provider=snapshot_provider),
        history=OrderHistoryPort(snapshot_provider=snapshot_provider),
        telemetry=NullTelemetryPort(),
        clock=UtcClockPort(),
        lifecycle=lifecycle_bus,
    )


def bind_extension_orderbook_reader(
    ports: ExtensionPorts,
    orderbook_reader: OrderbookReader | None,
) -> None:
    market_port = ports.market
    if isinstance(market_port, MarketDataPort):
        market_port.bind_orderbook_reader(orderbook_reader)
