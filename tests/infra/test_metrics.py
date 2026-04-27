from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.observability.metrics import MetricsRegistry


def test_metrics_registry_snapshot_captures_queue_ws_reconcile_and_gate_state() -> None:
    registry = MetricsRegistry(latency_buckets_ms=(10.0, 50.0, 100.0))
    timestamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    registry.set_queue_depth("trading", 3, capacity=10, retained_depth=2, paused=True, updated_at=timestamp)
    registry.set_ws_state(
        "market_ws",
        connected=False,
        subscribed_count=2,
        last_error="boom",
        reconnect_attempts=4,
        updated_at=timestamp,
    )
    registry.set_gauge("entry_signal_to_submit_ms", 65.0, updated_at=timestamp)
    registry.observe_latency("entry_signal_to_submit_ms", 42.5, updated_at=timestamp)
    registry.record_reconcile(
        17.5,
        started_at=timestamp,
        completed_at=timestamp,
        status="ok",
        trace_id="trace-reconcile",
        actions=3,
    )
    registry.set_trading_gate(False, reason="bootstrap", source="runtime", updated_at=timestamp)

    snapshot = registry.snapshot()

    assert snapshot.queue_depths[0].name == "trading"
    assert snapshot.queue_depths[0].depth == 3
    assert snapshot.queue_depths[0].retained_depth == 2
    assert snapshot.queue_depths[0].paused is True
    assert snapshot.ws_states[0].name == "market_ws"
    assert snapshot.ws_states[0].last_error == "boom"
    assert snapshot.reconcile.last_actions == 3
    assert snapshot.reconcile.last_duration_ms == 17.5
    assert snapshot.trading_gate.reason == "bootstrap"

    histogram_names = {histogram.name for histogram in snapshot.histograms}
    assert histogram_names == {"entry_signal_to_submit_ms", "reconcile_duration_ms"}

    entry_histogram = next(
        histogram for histogram in snapshot.histograms if histogram.name == "entry_signal_to_submit_ms"
    )
    assert entry_histogram.count == 1
    assert entry_histogram.min_ms == 42.5
    assert entry_histogram.max_ms == 42.5

    payload = snapshot.as_dict()
    assert payload["queue_depths"][0]["capacity"] == 10
    assert payload["ws_states"][0]["reconnect_attempts"] == 4
    assert payload["trading_gate"]["reason"] == "bootstrap"
    assert payload["reconcile"]["last_trace_id"] == "trace-reconcile"
