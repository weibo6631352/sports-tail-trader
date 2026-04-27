from __future__ import annotations

import asyncio

from polymarket_trader.app.market_service import MarketService
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.infra.outbox import LocalOutbox, build_domain_event_outbox_sink
from polymarket_trader.extension_api import ExtensionSpec, UniverseDecision
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.market_discovery_worker import MarketDiscoveryWorker


class _RejectingStrategy:
    @property
    def spec(self):
        return ExtensionSpec(name="rejecting")

    def select_market(self, market):
        return UniverseDecision.exclude(reason="market_out_of_universe")

    def size_entry(self, context):
        raise AssertionError("not used")

    def decide_entry(self, context):
        raise AssertionError("not used")

    def decide_exit(self, context):
        raise AssertionError("not used")

    def decide_recovery(self, context):
        raise AssertionError("not used")

    def should_keep_tracking(self, market, account_snapshot):
        return False

    def build_filtered_tracking_market(self, candidate_market, *, existing_market, reason):
        raise AssertionError("not used")


class _AcceptingStrategy:
    @property
    def spec(self):
        return ExtensionSpec(name="accepting")

    def select_market(self, market):
        return UniverseDecision.include(reason="accepted")

    def size_entry(self, context):
        raise AssertionError("not used")

    def decide_entry(self, context):
        raise AssertionError("not used")

    def decide_exit(self, context):
        raise AssertionError("not used")

    def decide_recovery(self, context):
        raise AssertionError("not used")

    def should_keep_tracking(self, market, account_snapshot):
        return True

    def build_filtered_tracking_market(self, candidate_market, *, existing_market, reason):
        return candidate_market


class _SwitchingStrategy:
    def __init__(self, *, selected: bool, keep_tracking: bool = False) -> None:
        self.selected = selected
        self.keep_tracking = keep_tracking

    @property
    def spec(self):
        return ExtensionSpec(name="switching")

    def select_market(self, market):
        if self.selected:
            return UniverseDecision.include(reason="accepted")
        return UniverseDecision.exclude(reason="market_out_of_universe")

    def size_entry(self, context):
        raise AssertionError("not used")

    def decide_entry(self, context):
        raise AssertionError("not used")

    def decide_exit(self, context):
        raise AssertionError("not used")

    def decide_recovery(self, context):
        raise AssertionError("not used")

    def should_keep_tracking(self, market, account_snapshot):
        return self.keep_tracking

    def build_filtered_tracking_market(self, candidate_market, *, existing_market, reason):
        return candidate_market


def test_market_discovery_worker_skips_untracked_filtered_out_markets() -> None:
    async def run() -> None:
        event_bus = EventBus()
        outbox = LocalOutbox(max_size=8)
        event_bus.bind_persistence_sink(build_domain_event_outbox_sink(outbox))
        worker = MarketDiscoveryWorker(
            market_service=MarketService(extension_hooks=_RejectingStrategy()),
            event_bus=event_bus,
        )

        events = await worker.ingest_source_page(
            {
                "events": [
                    {
                        "markets": [
                            {
                                "conditionId": "condition-1",
                                "slug": "sample-market-a",
                                "eventTitle": "Sample Threshold Event",
                                "question": "Will this project hit the target threshold?",
                                "tags": [{"label": "Crypto", "slug": "crypto"}],
                                "clobTokenIds": ["yes-1", "no-1"],
                                "orderPriceMinTickSize": "0.01",
                                "orderMinSize": "1",
                            }
                        ]
                    }
                ]
            },
            source="gamma",
            trace_id="trace-1",
        )

        assert events == []
        assert event_bus.snapshot().maintenance_queue_depth == 0
        try:
            await asyncio.wait_for(outbox.get(), timeout=0.05)
        except asyncio.TimeoutError:
            pass
        else:  # pragma: no cover - defensive assertion path
            raise AssertionError("untracked filtered-out markets should not reach outbox")

    asyncio.run(run())


def test_market_discovery_worker_skips_duplicate_market_payloads() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        worker = MarketDiscoveryWorker(
            market_service=MarketService(
                extension_hooks=_AcceptingStrategy(),
                registry=registry,
            ),
            event_bus=event_bus,
        )
        payload = {
            "markets": [
                {
                    "conditionId": "condition-1",
                    "slug": "sample-market-a",
                    "eventTitle": "Sample Threshold Event",
                    "question": "Will this project hit the target threshold?",
                    "tags": [{"label": "Crypto", "slug": "crypto"}],
                    "clobTokenIds": ["yes-1", "no-1"],
                    "orderPriceMinTickSize": "0.01",
                    "orderMinSize": "1",
                    "active": True,
                    "closed": False,
                }
            ]
        }

        first = await worker.ingest_source_page(payload, source="gamma.markets_keyset", trace_id="trace-1")
        second = await worker.ingest_source_page(payload, source="gamma.markets_keyset", trace_id="trace-2")

        assert len(first) == 1
        assert first[0].event_type == DomainEventType.MARKET_DISCOVERED
        assert first[0].payload["parse_status"] == "accepted"
        assert first[0].payload["parse_reason"] is None
        assert first[0].payload["parse_detail"] is None
        assert second == []
        assert event_bus.snapshot().maintenance_queue_depth == 1

    asyncio.run(run())


def test_market_discovery_worker_replays_duplicate_payload_when_tracking_state_changes() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        strategy = _SwitchingStrategy(selected=True, keep_tracking=False)
        worker = MarketDiscoveryWorker(
            market_service=MarketService(
                extension_hooks=strategy,
                registry=registry,
            ),
            event_bus=event_bus,
        )
        payload = {
            "markets": [
                {
                    "conditionId": "condition-1",
                    "slug": "sample-market-a",
                    "eventTitle": "Sample Threshold Event",
                    "question": "Will this project hit the target threshold?",
                    "tags": [{"label": "Crypto", "slug": "crypto"}],
                    "clobTokenIds": ["yes-1", "no-1"],
                    "orderPriceMinTickSize": "0.01",
                    "orderMinSize": "1",
                    "active": True,
                    "closed": False,
                }
            ]
        }

        first = await worker.ingest_source_page(payload, source="gamma.markets_keyset", trace_id="trace-1")
        strategy.selected = False
        second = await worker.ingest_source_page(payload, source="gamma.markets_keyset", trace_id="trace-2")

        assert len(first) == 1
        assert first[0].event_type == DomainEventType.MARKET_DISCOVERED
        assert len(second) == 1
        assert second[0].event_type == DomainEventType.MARKET_FILTERED_OUT
        assert second[0].payload["parse_status"] == "accepted"
        assert second[0].payload["parse_reason"] is None
        assert second[0].payload["parse_detail"] is None
        assert second[0].payload["extension_reason"] == "market_out_of_universe"
        assert registry.get_by_condition_id("condition-1") is None

    asyncio.run(run())


def test_market_discovery_worker_treats_fee_schedule_change_as_market_update() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        worker = MarketDiscoveryWorker(
            market_service=MarketService(
                extension_hooks=_AcceptingStrategy(),
                registry=registry,
            ),
            event_bus=event_bus,
        )
        first_payload = {
            "markets": [
                {
                    "conditionId": "condition-1",
                    "slug": "sample-market-a",
                    "eventTitle": "Sample Threshold Event",
                    "question": "Will this project hit the target threshold?",
                    "tags": [{"label": "Crypto", "slug": "crypto"}],
                    "clobTokenIds": ["yes-1", "no-1"],
                    "orderPriceMinTickSize": "0.01",
                    "orderMinSize": "1",
                    "active": True,
                    "closed": False,
                    "takerBaseFee": 1000,
                    "feeSchedule": {
                        "rate": "0.072",
                    },
                }
            ]
        }
        second_payload = {
            "markets": [
                {
                    "conditionId": "condition-1",
                    "slug": "sample-market-a",
                    "eventTitle": "Sample Threshold Event",
                    "question": "Will this project hit the target threshold?",
                    "tags": [{"label": "Crypto", "slug": "crypto"}],
                    "clobTokenIds": ["yes-1", "no-1"],
                    "orderPriceMinTickSize": "0.01",
                    "orderMinSize": "1",
                    "active": True,
                    "closed": False,
                    "takerBaseFee": 1000,
                    "feeSchedule": {
                        "rate": "0.050",
                    },
                }
            ]
        }

        first = await worker.ingest_source_page(first_payload, source="gamma.markets_keyset", trace_id="trace-1")
        second = await worker.ingest_source_page(second_payload, source="gamma.markets_keyset", trace_id="trace-2")

        assert len(first) == 1
        assert first[0].event_type == DomainEventType.MARKET_DISCOVERED
        assert len(second) == 1
        assert second[0].event_type == DomainEventType.MARKET_UPDATED
        market = registry.get_by_condition_id("condition-1")
        assert market is not None
        assert market.taker_base_fee_bps == 50
        assert market.fee_rate_bps == 50

    asyncio.run(run())
