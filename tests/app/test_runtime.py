from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.config import Settings
from polymarket_trader.main import (
    FullMarketDiscoveryState,
    _coalesce_reconcile_scope,
    _handle_market_ws_message,
    _is_reconcile_trigger,
    _publish_reconcile_trigger,
    _run_market_discovery_scan,
    build_runtime,
)
from polymarket_trader.runtime.event_bus import EventBus
from tests.helpers.markets import build_binary_market


def test_build_runtime_wires_m2_components() -> None:
    runtime = build_runtime(
        Settings(
            _env_file=None,
            extension_module="tests.helpers.demo_extension",
            portfolio_budget_usdc=Decimal("100"),
            max_order_usdc=Decimal("25"),
            max_market_usdc=Decimal("50"),
            max_total_usdc=Decimal("100"),
            max_open_orders=10,
        )
    )

    assert runtime.market_discovery_worker is not None
    assert runtime.extension.spec.name == "demo"
    assert runtime.market_service is not None
    assert runtime.market_ws_worker is not None
    assert runtime.user_ws_worker is not None
    assert runtime.trading_decision_service is not None
    assert runtime.trading_service is not None
    assert runtime.trading_decision_worker is not None
    assert runtime.account_state_store is not None
    assert runtime.account_state_store.snapshot().user_ws_connected is False
    assert runtime.account_state_store.snapshot().allow_new_entries is False
    assert runtime.account_state_store.snapshot().last_reconcile_at is None
    assert runtime.order_executor is not None
    assert runtime.reconcile_service is not None
    assert runtime.reconcile_worker is not None
    assert runtime.trading_decision_worker.priority == "P0"


def test_build_runtime_binds_market_event_outbox_sink() -> None:
    async def run() -> None:
        runtime = build_runtime(
            Settings(
                _env_file=None,
                extension_module="tests.helpers.demo_extension",
                portfolio_budget_usdc=Decimal("100"),
                max_order_usdc=Decimal("25"),
                max_market_usdc=Decimal("50"),
                max_total_usdc=Decimal("100"),
                max_open_orders=10,
            )
        )

        event = DomainEvent(
            trace_id="trace-market",
            event_type=DomainEventType.MARKET_DISCOVERED,
            event_id="event-market",
            market_slug="sample-market-a",
            condition_id="condition-sample",
            token_id="no-token-sample",
            reason="market_discovered",
            payload={"market": {"condition_id": "condition-sample"}},
        )
        await runtime.event_bus.publish(OutboxPriority.P2, event)

        queued = await asyncio.wait_for(runtime.outbox.get(), timeout=0.1)
        assert queued.event_id == event.event_id
        assert queued.event_type == DomainEventType.MARKET_DISCOVERED.value
        assert queued.payload["market"]["condition_id"] == "condition-sample"

    asyncio.run(run())


def test_build_runtime_binds_user_event_outbox_sink_with_trimmed_payload() -> None:
    async def run() -> None:
        runtime = build_runtime(
            Settings(
                _env_file=None,
                extension_module="tests.helpers.demo_extension",
                portfolio_budget_usdc=Decimal("100"),
                max_order_usdc=Decimal("25"),
                max_market_usdc=Decimal("50"),
                max_total_usdc=Decimal("100"),
                max_open_orders=10,
            )
        )

        event = DomainEvent(
            trace_id="trace-order",
            event_type=DomainEventType.ORDER_STATE_UPDATED,
            event_id="event-order",
            market_slug="sample-market-a",
            condition_id="condition-sample",
            token_id="no-token-sample",
            reason="order_update",
            payload={
                "order": {
                    "condition_id": "condition-sample",
                    "token_id": "no-token-sample",
                    "side": "BUY",
                    "order_type": "FAK",
                    "price": "0.43",
                },
                "snapshot": {"positions": []},
            },
        )
        await runtime.event_bus.publish(OutboxPriority.P0, event)

        queued = await asyncio.wait_for(runtime.outbox.get(), timeout=0.1)
        assert queued.event_id == event.event_id
        assert queued.event_type == DomainEventType.ORDER_STATE_UPDATED.value
        assert "order" in queued.payload
        assert "snapshot" not in queued.payload

    asyncio.run(run())


def test_build_runtime_binds_balance_event_outbox_sink() -> None:
    async def run() -> None:
        runtime = build_runtime(
            Settings(
                _env_file=None,
                extension_module="tests.helpers.demo_extension",
                portfolio_budget_usdc=Decimal("100"),
                max_order_usdc=Decimal("25"),
                max_market_usdc=Decimal("50"),
                max_total_usdc=Decimal("100"),
                max_open_orders=10,
            )
        )

        event = DomainEvent(
            trace_id="trace-balance",
            event_type=DomainEventType.BALANCE_UPDATED,
            event_id="event-balance",
            reason="balance_update",
            payload={
                "balance_usdc": "120",
                "allowance_usdc": "90",
                "user_ws_connected": True,
                "allow_new_entries": True,
            },
        )
        await runtime.event_bus.publish(OutboxPriority.P0, event)

        queued = await asyncio.wait_for(runtime.outbox.get(), timeout=0.1)
        assert queued.event_id == event.event_id
        assert queued.event_type == DomainEventType.BALANCE_UPDATED.value
        assert str(queued.payload["balance_usdc"]) == "120"
        assert str(queued.payload["allowance_usdc"]) == "90"

    asyncio.run(run())


def test_build_runtime_binds_strategy_orderbook_port() -> None:
    async def run() -> None:
        runtime = build_runtime(
            Settings(
                _env_file=None,
                extension_module="tests.helpers.demo_extension",
                portfolio_budget_usdc=Decimal("100"),
                max_order_usdc=Decimal("25"),
                max_market_usdc=Decimal("50"),
                max_total_usdc=Decimal("100"),
                max_open_orders=10,
            )
        )

        market = build_binary_market(
            condition_id="condition-1",
            market_slug="sample-market-a",
            no_token_id="no-token-1",
            yes_token_id="yes-token-1",
            category="Crypto",
            matched_keywords=("threshold", "target"),
            trading_status=TradingStatus.ELIGIBLE,
        )
        runtime.market_ws_worker.track_market(market)
        await runtime.market_ws_worker.handle_message(
            {
                "type": "best_bid_ask",
                "token_id": market.require_token_id("NO"),
                "best_bid": "0.55",
                "best_ask": "0.43",
                "best_bid_size": "100",
                "best_ask_size": "200",
            }
        )

        ports = getattr(runtime.extension, "ports", None)
        assert ports is not None
        assert ports.market is not None
        snapshot = ports.market.get_orderbook(market.require_token_id("NO"))
        assert snapshot is not None
        assert snapshot.best_ask == Decimal("0.43")

    asyncio.run(run())


def test_run_market_discovery_scan_advances_full_market_round_scan() -> None:
    class _StubEventPage:
        def __init__(self, payloads):
            self._payloads = tuple(payloads)

        def to_raw_market_events(self, *, source):
            return tuple(SimpleNamespace(payload=payload) for payload in self._payloads)

    class _StubGammaClient:
        def __init__(self) -> None:
            self.calls = []

        async def list_events_keyset_by_params(self, params, *, timeout_s=None):
            self.calls.append(dict(params))
            if len(self.calls) == 1:
                return (
                    (
                        _StubEventPage(
                            (
                                {
                                    "conditionId": "condition-1",
                                    "slug": "sample-market-a",
                                    "eventTitle": "Sample Event A",
                                },
                            )
                        ),
                    ),
                    "cursor-2",
                )
            return (
                (
                    _StubEventPage(
                        (
                            {
                                "conditionId": "condition-2",
                                "slug": "sample-market-b",
                                "eventTitle": "Sample Event B",
                            },
                        )
                    ),
                ),
                None,
            )

    class _StubDiscoveryWorker:
        def __init__(self) -> None:
            self.calls = []
            self.last_failure = None

        async def ingest_source_page(self, payload, *, source, trace_id):
            self.calls.append((payload, source, trace_id))

        def record_failure(self, *, source, reason):
            self.calls.append(("failure", source, reason))

        def should_retry(self):
            return False

        def mark_scan_success(self):
            self.last_failure = None

    class _StubSupervisor:
        def heartbeat_worker(self, *args, **kwargs):
            return None

        def mark_worker_error(self, *args, **kwargs):
            return None

    class _StubMetrics:
        def set_queue_depth(self, *args, **kwargs):
            return None

        def set_ws_state(self, *args, **kwargs):
            return None

        def set_gauge(self, *args, **kwargs):
            return None

        def inc_counter(self, *args, **kwargs):
            return None

        def mark_timestamp(self, *args, **kwargs):
            return None

    class _StubEventBus:
        def snapshot(self):
            return SimpleNamespace(
                trading_queue_depth=0,
                trading_queue_capacity=1,
                trading_retained_depth=0,
                maintenance_queue_depth=0,
                maintenance_queue_capacity=1,
                maintenance_retained_depth=0,
                persistence_queue_depth=0,
                persistence_queue_capacity=1,
                persistence_retained_depth=0,
                low_priority_paused=False,
            )

    class _StubMarketWsWorker:
        def status_snapshot(self):
            return SimpleNamespace(
                last_result=None,
                subscription_count=0,
                last_message_at=None,
                last_error=None,
            )

    class _StubUserWsWorker:
        def status_snapshot(self):
            return SimpleNamespace(
                connected=False,
                subscription_count=0,
                last_message_at=None,
                last_result=None,
                last_error=None,
            )

    class _StubReconcileWorker:
        def status_snapshot(self):
            return SimpleNamespace(last_completed_at=None)

    class _StubPersistenceWorker:
        def snapshot(self):
            return SimpleNamespace(
                outbox_depth=0,
                outbox_retained_depth=0,
                outbox_dead_letter_depth=0,
                retried_events=0,
            )

    async def run() -> None:
        gamma = _StubGammaClient()
        discovery_worker = _StubDiscoveryWorker()
        runtime = SimpleNamespace(
            supervisor=_StubSupervisor(),
            gamma_client=gamma,
            market_discovery_worker=discovery_worker,
            market_discovery_scan=FullMarketDiscoveryState(),
            event_bus=_StubEventBus(),
            metrics=_StubMetrics(),
            market_ws_worker=_StubMarketWsWorker(),
            user_ws_worker=_StubUserWsWorker(),
            reconcile_worker=_StubReconcileWorker(),
            persistence_worker=_StubPersistenceWorker(),
        )

        await _run_market_discovery_scan(runtime)

        assert gamma.calls[0]["active"] is True
        assert gamma.calls[0]["closed"] is False
        assert gamma.calls[0]["limit"] == 50
        assert "after_cursor" not in gamma.calls[0]
        assert gamma.calls[1]["after_cursor"] == "cursor-2"
        assert len(discovery_worker.calls) == 2
        payload, source, trace_id = discovery_worker.calls[0]
        assert payload["markets"][0]["slug"] == "sample-market-a"
        assert payload["markets"][0]["eventTitle"] == "Sample Event A"
        assert source == "gamma.events_keyset"
        assert trace_id.startswith("market-discovery-round-1-")
        assert runtime.market_discovery_scan.after_cursor is None
        assert runtime.market_discovery_scan.round_id == 2
        assert runtime.market_discovery_scan.last_round_completed_at is not None
        assert runtime.market_discovery_scan.last_completed_round_pages == 2
        assert runtime.market_discovery_scan.last_completed_round_markets == 2

    asyncio.run(run())


def test_run_market_discovery_scan_uses_extension_discovery_queries_with_framework_cursor() -> None:
    from polymarket_trader.extension_api import DiscoveryQuery

    class _StubEventPage:
        def __init__(self, payloads):
            self._payloads = tuple(payloads)

        def to_raw_market_events(self, *, source):
            return tuple(SimpleNamespace(payload=payload) for payload in self._payloads)

    class _StubGammaClient:
        def __init__(self) -> None:
            self.calls = []

        async def list_events_keyset_by_params(self, params, *, timeout_s=None):
            self.calls.append(dict(params))
            if params["title_search"] == "alpha":
                if params.get("after_cursor") == "alpha-cursor-2":
                    return ((_StubEventPage(({"slug": "alpha-market-page-2"},)),), None)
                return ((_StubEventPage(({"slug": "alpha-market"},)),), "alpha-cursor-2")
            return ((_StubEventPage(({"slug": "beta-market"},)),), None)

    class _StubHooks:
        def discovery_queries(self):
            return (
                DiscoveryQuery(name="alpha", params={"title_search": "alpha", "limit": 999}),
                DiscoveryQuery(
                    name="beta",
                    params={"title_search": "beta", "after_cursor": "extension-owned"},
                ),
            )

    class _StubDiscoveryWorker:
        last_failure = None

        def __init__(self) -> None:
            self.calls = []

        async def ingest_source_page(self, payload, *, source, trace_id):
            self.calls.append((payload, source, trace_id))

        def record_failure(self, *, source, reason):
            self.calls.append(("failure", source, reason))

        def should_retry(self):
            return False

        def mark_scan_success(self):
            return None

    class _StubSupervisor:
        def heartbeat_worker(self, *args, **kwargs):
            return None

        def mark_worker_error(self, *args, **kwargs):
            return None

    class _StubMetrics:
        def set_queue_depth(self, *args, **kwargs):
            return None

        def set_ws_state(self, *args, **kwargs):
            return None

        def set_gauge(self, *args, **kwargs):
            return None

        def inc_counter(self, *args, **kwargs):
            return None

        def mark_timestamp(self, *args, **kwargs):
            return None

    class _StubEventBus:
        def snapshot(self):
            return SimpleNamespace(
                trading_queue_depth=0,
                trading_queue_capacity=1,
                trading_retained_depth=0,
                maintenance_queue_depth=0,
                maintenance_queue_capacity=1,
                maintenance_retained_depth=0,
                persistence_queue_depth=0,
                persistence_queue_capacity=1,
                persistence_retained_depth=0,
                low_priority_paused=False,
            )

    class _StubWorkerWithStatus:
        def status_snapshot(self):
            return SimpleNamespace(
                connected=False,
                last_result=None,
                subscription_count=0,
                last_message_at=None,
                last_error=None,
            )

    class _StubReconcileWorker:
        def status_snapshot(self):
            return SimpleNamespace(last_completed_at=None)

    class _StubPersistenceWorker:
        def snapshot(self):
            return SimpleNamespace(
                outbox_depth=0,
                outbox_retained_depth=0,
                outbox_dead_letter_depth=0,
                retried_events=0,
            )

    async def run() -> None:
        gamma = _StubGammaClient()
        discovery_worker = _StubDiscoveryWorker()
        runtime = SimpleNamespace(
            extension=SimpleNamespace(hooks=_StubHooks()),
            supervisor=_StubSupervisor(),
            gamma_client=gamma,
            market_discovery_worker=discovery_worker,
            market_discovery_scan=FullMarketDiscoveryState(),
            event_bus=_StubEventBus(),
            metrics=_StubMetrics(),
            market_ws_worker=_StubWorkerWithStatus(),
            user_ws_worker=_StubWorkerWithStatus(),
            reconcile_worker=_StubReconcileWorker(),
            persistence_worker=_StubPersistenceWorker(),
        )

        await _run_market_discovery_scan(runtime)

        assert gamma.calls == [
            {
                "active": True,
                "closed": False,
                "title_search": "alpha",
                "limit": 50,
            },
            {
                "active": True,
                "closed": False,
                "title_search": "beta",
                "limit": 50,
            },
        ]
        assert runtime.market_discovery_scan.query_cursors == {"alpha": "alpha-cursor-2"}
        assert runtime.market_discovery_scan.completed_query_names == {"beta"}
        assert [call[0]["markets"][0]["slug"] for call in discovery_worker.calls] == [
            "alpha-market",
            "beta-market",
        ]

        await _run_market_discovery_scan(runtime)

        assert gamma.calls[2] == {
            "active": True,
            "closed": False,
            "title_search": "alpha",
            "limit": 50,
            "after_cursor": "alpha-cursor-2",
        }
        assert runtime.market_discovery_scan.query_cursors == {}
        assert runtime.market_discovery_scan.completed_query_names == set()
        assert runtime.market_discovery_scan.round_id == 2

    asyncio.run(run())


def test_handle_market_ws_message_routes_new_market_through_discovery_before_ws_processing() -> None:
    class _StubDiscoveryWorker:
        def __init__(self) -> None:
            self.calls = []

        async def ingest_ws_new_market(self, payload, *, trace_id=None):
            self.calls.append(("discovery", dict(payload), trace_id))
            return []

    class _StubMarketWsWorker:
        def __init__(self) -> None:
            self.calls = []

        async def handle_message(self, payload, *, source="market_ws"):
            self.calls.append(("ws", dict(payload), source))
            return []

    async def run() -> None:
        runtime = SimpleNamespace(
            market_discovery_worker=_StubDiscoveryWorker(),
            market_ws_worker=_StubMarketWsWorker(),
        )

        await _handle_market_ws_message(
            runtime,
            {
                "event_type": "new_market",
                "asset_id": "no-token-1",
                "market": "condition-1",
            },
        )

        assert runtime.market_discovery_worker.calls[0][0] == "discovery"
        assert runtime.market_ws_worker.calls[0][0] == "ws"

    asyncio.run(run())


def test_publish_reconcile_trigger_enqueues_maintenance_event(monkeypatch) -> None:
    async def run() -> None:
        event_bus = EventBus()
        runtime = SimpleNamespace(event_bus=event_bus)
        monkeypatch.setattr("polymarket_trader.main._sync_runtime_metrics", lambda _runtime: None)

        await _publish_reconcile_trigger(runtime, source="scheduled")

        published = await asyncio.wait_for(event_bus.next_maintenance_event(), timeout=0.1)
        assert published.event_type == "reconcile_scheduled"
        assert published.reason == "scheduled"
        assert published.payload["source"] == "scheduled"
        assert published.condition_id is None

    asyncio.run(run())


def test_coalesce_reconcile_scope_merges_condition_ids_for_market_events() -> None:
    first = DomainEvent(
        trace_id="trace-market-1",
        event_type=DomainEventType.MARKET_DISCOVERED,
        event_id="event-market-1",
        condition_id="condition-1",
        reason="discovered",
    )
    second = DomainEvent(
        trace_id="trace-market-2",
        event_type=DomainEventType.MARKET_UPDATED,
        event_id="event-market-2",
        condition_id="condition-2",
        reason="updated",
    )

    source, trigger, condition_ids = _coalesce_reconcile_scope((first, second))

    assert source == "event-batch:2"
    assert trigger is second
    assert condition_ids == ("condition-1", "condition-2")


def test_coalesce_reconcile_scope_falls_back_to_full_reconcile_when_any_trigger_lacks_condition_id() -> None:
    market_event = DomainEvent(
        trace_id="trace-market-1",
        event_type=DomainEventType.MARKET_UPDATED,
        event_id="event-market-1",
        condition_id="condition-1",
        reason="updated",
    )
    scheduled_trigger = DomainEvent(
        trace_id="trace-scheduled",
        event_type="reconcile_scheduled",
        event_id="event-scheduled",
        reason="scheduled",
    )

    source, trigger, condition_ids = _coalesce_reconcile_scope((market_event, scheduled_trigger))

    assert source == "event-batch:2"
    assert trigger is scheduled_trigger
    assert condition_ids is None


def test_is_reconcile_trigger_ignores_orderbook_snapshots_and_accepts_market_events() -> None:
    orderbook_event = DomainEvent(
        trace_id="trace-orderbook",
        event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
        event_id="event-orderbook",
        condition_id="condition-1",
        reason="snapshot",
    )
    market_event = DomainEvent(
        trace_id="trace-market",
        event_type=DomainEventType.MARKET_UPDATED,
        event_id="event-market",
        condition_id="condition-1",
        reason="market_update",
    )
    scheduled_trigger = DomainEvent(
        trace_id="trace-scheduled",
        event_type="reconcile_scheduled",
        event_id="event-scheduled",
        reason="scheduled",
    )

    assert _is_reconcile_trigger(orderbook_event) is False
    assert _is_reconcile_trigger(market_event) is True
    assert _is_reconcile_trigger(scheduled_trigger) is True
