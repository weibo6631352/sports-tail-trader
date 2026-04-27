from __future__ import annotations

import asyncio
from decimal import Decimal

from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.market_ws_worker import MarketWsWorker
from tests.helpers.markets import build_binary_market


def _market() -> Market:
    return build_binary_market(
        condition_id="condition",
        market_slug="sample-market-a",
        no_token_id="no-token",
        yes_token_id="yes-token",
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        category="Crypto",
        matched_keywords=("sample", "market", "threshold"),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _no_token_id(market: Market) -> str:
    return market.require_token_id("NO")


def _yes_token_id(market: Market) -> str:
    return market.require_token_id("YES")


def test_market_ws_worker_emits_orderbook_updates_to_trading_lane() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        worker = MarketWsWorker(event_bus=event_bus, registry=registry)
        market = _market()
        worker.track_market(market)

        first_events = await worker.handle_message(
            {
                "type": "best_bid_ask",
                "token_id": _no_token_id(market),
                "best_bid": "0.55",
                "best_ask": "0.43",
                "best_bid_size": "100",
                "best_ask_size": "200",
            }
        )
        second_events = await worker.handle_message(
            {
                "type": "best_bid_ask",
                "token_id": _no_token_id(market),
                "best_bid": "0.56",
                "best_ask": "0.59",
                "best_bid_size": "100",
                "best_ask_size": "200",
            }
        )

        assert [str(event.event_type) for event in first_events] == [
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]
        assert [str(event.event_type) for event in second_events] == [
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]
        assert worker.status_snapshot().tracked_token_ids == (
            _no_token_id(market),
            market.require_token_id("YES"),
        )

        snapshot_event = await event_bus.next_trading_event()
        assert snapshot_event.event_type == DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED

    asyncio.run(run())


def test_market_ws_worker_builds_official_subscription_payload() -> None:
    worker = MarketWsWorker()

    payload = worker.build_subscription_request(("no-token-1", "no-token-2"))

    assert payload == {
        "assets_ids": ["no-token-1", "no-token-2"],
        "type": "market",
        "custom_feature_enabled": True,
    }


def test_market_ws_worker_updates_yes_side_snapshot() -> None:
    async def run() -> None:
        worker = MarketWsWorker()
        market = _market()
        worker.track_market(market)

        events = await worker.handle_message(
            {
                "type": "best_bid_ask",
                "token_id": market.require_token_id("YES"),
                "best_bid": "0.95",
                "best_ask": "0.99",
                "best_bid_size": "80",
                "best_ask_size": "120",
            }
        )

        snapshot = worker.snapshot(market.require_token_id("YES"))
        assert snapshot is not None
        assert snapshot.best_bid == Decimal("0.95")
        assert snapshot.best_ask == Decimal("0.99")
        assert [str(event.event_type) for event in events] == [
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]

    asyncio.run(run())


def test_market_ws_worker_updates_tick_size_in_registry() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        worker = MarketWsWorker(registry=registry)
        market = _market()
        worker.track_market(market)

        await worker.handle_message(
            {
                "type": "tick_size_change",
                "token_id": _no_token_id(market),
                "tick_size": "0.02",
            }
        )

        updated = registry.get_by_condition_id(market.condition_id)
        assert updated is not None
        assert updated.tick_size == Decimal("0.02")

    asyncio.run(run())


def test_market_ws_worker_handles_official_price_change_batch_payload() -> None:
    async def run() -> None:
        worker = MarketWsWorker()
        market = _market()
        worker.track_market(market)

        events = await worker.handle_message(
            {
                "event_type": "price_change",
                "market": market.condition_id,
                "price_changes": [
                    {
                        "asset_id": _no_token_id(market),
                        "price": "0.59",
                        "size": "200",
                        "side": "SELL",
                        "best_bid": "0.55",
                        "best_ask": "0.59",
                    }
                ],
                "timestamp": "1757908892351",
            }
        )

        snapshot = worker.snapshot(_no_token_id(market))
        assert snapshot is not None
        assert snapshot.best_bid == Decimal("0.55")
        assert snapshot.best_ask == Decimal("0.59")
        assert [str(event.event_type) for event in events] == [
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]

    asyncio.run(run())


def test_market_ws_worker_handles_official_market_resolved_payload_with_assets_ids() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        worker = MarketWsWorker(registry=registry)
        market = _market()
        worker.track_market(market)

        events = await worker.handle_message(
            {
                "event_type": "market_resolved",
                "market": market.condition_id,
                "assets_ids": [market.require_token_id("YES"), _no_token_id(market)],
            }
        )

        assert [str(event.event_type) for event in events] == [
            DomainEventType.MARKET_RESOLVED_OR_DISABLED.value,
        ]
        assert worker.status_snapshot().resolved_token_ids == (
            _no_token_id(market),
            market.require_token_id("YES"),
        )
        updated = registry.get_by_condition_id(market.condition_id)
        assert updated is not None
        assert updated.trading_status == TradingStatus.RESOLVED

    asyncio.run(run())


def test_market_ws_worker_writes_market_fee_schedule_from_new_market_message() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        worker = MarketWsWorker(event_bus=event_bus, registry=registry)
        market = _market()
        worker.track_market(market)

        events = await worker.handle_message(
            {
                "event_type": "new_market",
                "market": market.condition_id,
                "asset_id": _no_token_id(market),
                "fees_enabled": True,
                "fee_schedule": {
                    "rate": "0.02",
                },
            }
        )

        updated = registry.get_by_condition_id(market.condition_id)
        assert updated is not None
        assert updated.fees_enabled is True
        assert updated.taker_base_fee_bps == 20
        assert [str(event.event_type) for event in events] == [
            DomainEventType.MARKET_UPDATED.value,
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]

        published = await event_bus.next_maintenance_event()
        assert published.event_type == DomainEventType.MARKET_UPDATED
        assert published.payload["market"]["fees"]["taker_base_fee_bps"] == 20

    asyncio.run(run())


def test_market_ws_worker_does_not_overwrite_fee_schedule_with_last_trade_fee_rate() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        worker = MarketWsWorker(event_bus=event_bus, registry=registry)
        market = _market().with_fee_schedule(fees_enabled=True, taker_base_fee_bps=72).with_fee_rate(72)
        worker.track_market(market)

        events = await worker.handle_message(
            {
                "event_type": "last_trade_price",
                "market": market.condition_id,
                "asset_id": _no_token_id(market),
                "last_trade_price": "0.58",
                "fee_rate_bps": "1000",
                "timestamp": "1757908892351",
            }
        )

        updated = registry.get_by_condition_id(market.condition_id)
        snapshot = worker.snapshot(_no_token_id(market))
        assert updated is not None
        assert updated.taker_base_fee_bps == 72
        assert updated.fee_rate_bps == 72
        assert snapshot is not None
        assert snapshot.last_trade_price == Decimal("0.58")
        assert [str(event.event_type) for event in events] == [
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]
        assert event_bus.snapshot().maintenance_queue_depth == 0

    asyncio.run(run())


def test_market_ws_worker_writes_fee_rate_from_last_trade_price_message() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        worker = MarketWsWorker(event_bus=event_bus, registry=registry)
        market = _market()
        worker.track_market(market)

        events = await worker.handle_message(
            {
                "event_type": "last_trade_price",
                "market": market.condition_id,
                "asset_id": _no_token_id(market),
                "last_trade_price": "0.58",
                "fee_rate_bps": "125",
                "timestamp": "1757908892351",
            }
        )

        updated = registry.get_by_condition_id(market.condition_id)
        snapshot = worker.snapshot(_no_token_id(market))
        assert updated is not None
        assert updated.fee_rate_bps == 125
        assert updated.fee_rate_updated_at is not None
        assert snapshot is not None
        assert snapshot.last_trade_price == Decimal("0.58")
        assert [str(event.event_type) for event in events] == [
            DomainEventType.MARKET_UPDATED.value,
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]

        published = await event_bus.next_maintenance_event()
        assert published.event_type == DomainEventType.MARKET_UPDATED
        assert published.payload["market"]["fees"]["fee_rate_bps"] == 125

    asyncio.run(run())


def test_market_ws_worker_overwrites_latest_snapshot() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        worker = MarketWsWorker(event_bus=event_bus, registry=registry)
        market = _market()
        worker.track_market(market)

        below_threshold_events = await worker.handle_message(
            {
                "type": "best_bid_ask",
                "token_id": _no_token_id(market),
                "best_bid": "0.57",
                "best_ask": "0.61",
                "best_bid_size": "100",
                "best_ask_size": "200",
            }
        )
        trigger_events = await worker.handle_message(
            {
                "type": "best_bid_ask",
                "token_id": _no_token_id(market),
                "best_bid": "0.56",
                "best_ask": "0.43",
                "best_bid_size": "110",
                "best_ask_size": "210",
            }
        )
        followup_events = await worker.handle_message(
            {
                "type": "best_bid_ask",
                "token_id": _no_token_id(market),
                "best_bid": "0.55",
                "best_ask": "0.59",
                "best_bid_size": "120",
                "best_ask_size": "220",
            }
        )

        assert [str(event.event_type) for event in below_threshold_events] == [
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]
        assert [str(event.event_type) for event in trigger_events] == [
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]
        assert [str(event.event_type) for event in followup_events] == [
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]

        snapshot = worker.snapshot(_no_token_id(market))
        assert snapshot is not None
        assert snapshot.best_bid == Decimal("0.55")
        assert snapshot.best_ask == Decimal("0.59")
        assert worker.status_snapshot().tracked_token_ids == (
            _no_token_id(market),
            market.require_token_id("YES"),
        )

    asyncio.run(run())


def test_market_ws_worker_routes_orderbook_updates_to_trading_queue() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        worker = MarketWsWorker(event_bus=event_bus, registry=registry)
        market = _market()
        worker.track_market(market)

        events = await worker.handle_message(
            {
                "type": "best_bid_ask",
                "token_id": _no_token_id(market),
                "best_bid": "0.57",
                "best_ask": "0.61",
                "best_bid_size": "100",
                "best_ask_size": "200",
            }
        )

        assert [str(event.event_type) for event in events] == [
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
        ]
        assert worker.status_snapshot().tracked_token_ids == (
            _no_token_id(market),
            market.require_token_id("YES"),
        )
        assert event_bus.trading_queue_depth() == 1
        assert event_bus.maintenance_queue_depth() == 0

    asyncio.run(run())
