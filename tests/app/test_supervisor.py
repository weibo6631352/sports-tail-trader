from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.observability.metrics import MetricsRegistry
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.status import RuntimePhase, trading_gate_reason
from polymarket_trader.runtime.supervisor import Supervisor


@dataclass(frozen=True, slots=True)
class _ReadyRuntimeContext:
    event_bus: EventBus
    supervisor: Supervisor
    metrics: MetricsRegistry
    account_state_store: AccountStateStore


def _build_ready_runtime(
    *,
    event_bus: EventBus,
    metrics_provider,
) -> _ReadyRuntimeContext:
    now = datetime.now(timezone.utc)
    account_state_store = AccountStateStore()
    account_state_store.update_balances(
        balance_usdc=Decimal("100"),
        allowance_usdc=Decimal("100"),
    )
    account_state_store.mark_user_ws_connected(True)
    account_state_store.mark_reconciled(now)

    supervisor = Supervisor(
        event_bus=event_bus,
        settings_readiness={"ready_to_trade": True, "warnings": ()},
        scheduler_snapshot_provider=lambda: {"jobs": []},
        account_snapshot_provider=account_state_store.snapshot,
        market_ws_snapshot_provider=lambda: {"connected": True},
        user_ws_snapshot_provider=lambda: {"connected": True},
        reconcile_snapshot_provider=lambda: {"last_reconcile_at": now.isoformat()},
        persistence_snapshot_provider=lambda: {"outbox_depth": 0},
        metrics_snapshot_provider=metrics_provider,
        trading_queue_warn_depth=2,
        entry_signal_to_submit_warn_ms=500,
        outbox_depth_warn=10,
        reconcile_stale_after_seconds=300,
    )
    supervisor.mark_db_ready(True)
    supervisor.mark_trading_client_ready(True)
    supervisor.set_phase(RuntimePhase.WORKERS_STARTED)
    return _ReadyRuntimeContext(
        event_bus=event_bus,
        supervisor=supervisor,
        metrics=MetricsRegistry(),
        account_state_store=account_state_store,
    )


def test_supervisor_pauses_low_priority_when_trading_queue_backlog_exceeds_threshold() -> None:
    async def run() -> None:
        event_bus = EventBus(trading_capacity=8, maintenance_capacity=4, persistence_capacity=4)
        context = _build_ready_runtime(
            event_bus=event_bus,
            metrics_provider=lambda: {"gauges": {}},
        )

        await event_bus.publish(
            "P0",
            DomainEvent(
                trace_id="trace-1",
                event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
                event_id="event-1",
                reason="orderbook",
            ),
        )
        await event_bus.publish(
            "P0",
            DomainEvent(
                trace_id="trace-2",
                event_type=DomainEventType.RISK_CHECK_PASSED,
                event_id="event-2",
                reason="risk",
            ),
        )

        snapshot = await context.supervisor.refresh()
        assert context.event_bus.low_priority_paused() is True
        assert snapshot.low_priority_paused is True
        assert snapshot.automatic_trading_enabled is False
        assert snapshot.readiness is not None
        assert snapshot.readiness.ready is False
        assert "p0_backpressure" in snapshot.readiness.blocking_reasons
        assert snapshot.degraded_reason == "p0_backpressure"
        assert trading_gate_reason(snapshot) == "p0_backpressure"

        assert await context.event_bus.next_trading_event()

        resumed_snapshot = await context.supervisor.refresh()
        assert context.event_bus.low_priority_paused() is False
        assert resumed_snapshot.low_priority_paused is False
        assert resumed_snapshot.readiness is not None
        assert resumed_snapshot.readiness.ready is True
        assert resumed_snapshot.automatic_trading_enabled is True
        assert resumed_snapshot.degraded_reason is None
        assert trading_gate_reason(resumed_snapshot) is None

    asyncio.run(run())


def test_supervisor_pauses_low_priority_on_entry_latency_metric_threshold() -> None:
    async def run() -> None:
        event_bus = EventBus()
        metrics = MetricsRegistry()
        metrics.set_gauge("entry_signal_to_submit_ms", 650)

        def metrics_provider() -> dict[str, object]:
            snapshot = metrics.snapshot()
            entry_metric = next(
                (gauge for gauge in snapshot.gauges if gauge.name == "entry_signal_to_submit_ms"),
                None,
            )
            return {
                "gauges": {
                    "entry_signal_to_submit_ms": {
                        "value": 0.0 if entry_metric is None else entry_metric.value,
                    }
                }
            }

        context = _build_ready_runtime(
            event_bus=event_bus,
            metrics_provider=metrics_provider,
        )

        snapshot = await context.supervisor.refresh()
        assert context.event_bus.low_priority_paused() is True
        assert snapshot.low_priority_paused is True
        assert snapshot.automatic_trading_enabled is False
        assert snapshot.readiness is not None
        assert snapshot.readiness.ready is False
        assert "p0_backpressure" in snapshot.readiness.blocking_reasons
        assert snapshot.degraded_reason == "p0_backpressure"

        metrics.set_gauge("entry_signal_to_submit_ms", 120)
        recovered = await context.supervisor.refresh()
        assert context.event_bus.low_priority_paused() is False
        assert recovered.low_priority_paused is False
        assert recovered.readiness is not None
        assert recovered.readiness.ready is True
        assert recovered.automatic_trading_enabled is True
        assert recovered.degraded_reason is None

    asyncio.run(run())
