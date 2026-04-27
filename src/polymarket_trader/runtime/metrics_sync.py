from __future__ import annotations

from typing import Any


def sync_runtime_metrics(runtime: Any) -> None:
    queue_snapshot = runtime.event_bus.snapshot()
    runtime.metrics.set_queue_depth(
        "trading_queue_depth",
        queue_snapshot.trading_queue_depth,
        capacity=queue_snapshot.trading_queue_capacity,
        retained_depth=queue_snapshot.trading_retained_depth,
        paused=False,
    )
    runtime.metrics.set_queue_depth(
        "maintenance_queue_depth",
        queue_snapshot.maintenance_queue_depth,
        capacity=queue_snapshot.maintenance_queue_capacity,
        retained_depth=queue_snapshot.maintenance_retained_depth,
        paused=queue_snapshot.low_priority_paused,
    )
    runtime.metrics.set_queue_depth(
        "persistence_queue_depth",
        queue_snapshot.persistence_queue_depth,
        capacity=queue_snapshot.persistence_queue_capacity,
        retained_depth=queue_snapshot.persistence_retained_depth,
        paused=queue_snapshot.low_priority_paused,
    )

    market_ws = runtime.market_ws_worker.status_snapshot()
    market_last_event = None
    if market_ws.last_result is not None and market_ws.last_result.event_types:
        market_last_event = market_ws.last_result.event_types[-1]
    runtime.metrics.set_ws_state(
        "market_ws",
        connected=market_ws.last_error is None,
        subscribed_count=market_ws.subscription_count,
        last_message_at=market_ws.last_message_at,
        last_event_type=market_last_event,
        last_error=market_ws.last_error,
    )

    user_ws = runtime.user_ws_worker.status_snapshot()
    runtime.metrics.set_ws_state(
        "user_ws",
        connected=user_ws.connected,
        subscribed_count=user_ws.subscription_count,
        last_message_at=user_ws.last_message_at,
        last_event_type=None if user_ws.last_result is None else user_ws.last_result.message_type,
        last_error=user_ws.last_error,
        reconnect_attempts=0,
    )

    reconcile = runtime.reconcile_worker.status_snapshot()
    if reconcile.last_completed_at is not None:
        runtime.metrics.mark_timestamp("last_reconcile_at", at=reconcile.last_completed_at)

    discovery = runtime.market_discovery_scan
    runtime.metrics.set_gauge("market_discovery_round_id", float(discovery.round_id))
    runtime.metrics.set_gauge(
        "market_discovery_pages_scanned_in_round",
        float(discovery.pages_scanned_in_round),
    )
    runtime.metrics.set_gauge(
        "market_discovery_markets_seen_in_round",
        float(discovery.markets_seen_in_round),
    )
    runtime.metrics.set_gauge(
        "market_discovery_last_page_size",
        float(discovery.last_page_size),
    )
    runtime.metrics.set_gauge(
        "market_discovery_after_cursor_present",
        1.0 if discovery.after_cursor is not None or discovery.query_cursors else 0.0,
    )
    runtime.metrics.set_gauge(
        "market_discovery_active_cursor_count",
        float(len(discovery.query_cursors)),
    )
    runtime.metrics.set_gauge(
        "market_discovery_completed_query_count",
        float(len(discovery.completed_query_names)),
    )
    runtime.metrics.set_gauge(
        "market_discovery_last_tick_requests",
        float(discovery.last_tick_requests),
    )
    runtime.metrics.set_gauge(
        "market_discovery_last_tick_markets",
        float(discovery.last_tick_markets),
    )
    runtime.metrics.set_gauge(
        "market_discovery_consecutive_failures",
        float(discovery.consecutive_failures),
    )
    if discovery.last_round_completed_at is not None:
        runtime.metrics.mark_timestamp(
            "market_discovery_last_round_completed_at",
            at=discovery.last_round_completed_at,
        )

    persistence = runtime.persistence_worker.snapshot()
    runtime.metrics.set_gauge("outbox_depth", persistence.outbox_depth)
    runtime.metrics.set_gauge("outbox_retained_depth", persistence.outbox_retained_depth)
    runtime.metrics.set_gauge("outbox_dead_letter_depth", persistence.outbox_dead_letter_depth)
    runtime.metrics.set_gauge("persistence_retry_count", persistence.retried_events)
