from __future__ import annotations
from collections.abc import Callable, Mapping
from dataclasses import is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.status import (
    ReadinessSnapshot,
    RuntimePhase,
    RuntimeSnapshot,
    SchedulerSnapshot,
    WorkerHealth,
    WorkerLifecycleState,
)

SnapshotProvider = Callable[[], Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "as_dict") and callable(value.as_dict):
        result = value.as_dict()
        if isinstance(result, Mapping):
            return result
    if is_dataclass(value):
        from dataclasses import asdict

        return asdict(value)
    if isinstance(value, Mapping):
        return value
    return {"value": value}


def _bool(mapping: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = mapping.get(key, default)
    if isinstance(value, bool):
        return value
    return bool(value)


def _int(mapping: Mapping[str, Any], key: str, default: int = 0) -> int:
    value = mapping.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _datetime(mapping: Mapping[str, Any], key: str) -> datetime | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Supervisor:
    """Coordinates worker lifecycle, readiness, and low-priority degradation.

    Supervisor 不直接执行交易逻辑。它只读取各 worker 的轻量快照，决定系统是否允许
    自动下单，以及在 P0 压力升高时是否暂停 P2/P3。
    """

    def __init__(
        self,
        *,
        event_bus: EventBus,
        settings_readiness: Any,
        scheduler_snapshot_provider: SnapshotProvider | None = None,
        account_snapshot_provider: SnapshotProvider | None = None,
        market_ws_snapshot_provider: SnapshotProvider | None = None,
        user_ws_snapshot_provider: SnapshotProvider | None = None,
        reconcile_snapshot_provider: SnapshotProvider | None = None,
        persistence_snapshot_provider: SnapshotProvider | None = None,
        metrics_snapshot_provider: SnapshotProvider | None = None,
        sse_snapshot_provider: SnapshotProvider | None = None,
        trading_queue_warn_depth: int = 100,
        entry_signal_to_submit_warn_ms: int = 500,
        outbox_depth_warn: int = 1000,
        reconcile_stale_after_seconds: int = 300,
    ) -> None:
        self._event_bus = event_bus
        self._settings_readiness = settings_readiness
        self._scheduler_snapshot_provider = scheduler_snapshot_provider
        self._account_snapshot_provider = account_snapshot_provider
        self._market_ws_snapshot_provider = market_ws_snapshot_provider
        self._user_ws_snapshot_provider = user_ws_snapshot_provider
        self._reconcile_snapshot_provider = reconcile_snapshot_provider
        self._persistence_snapshot_provider = persistence_snapshot_provider
        self._metrics_snapshot_provider = metrics_snapshot_provider
        self._sse_snapshot_provider = sse_snapshot_provider
        self._trading_queue_warn_depth = max(0, trading_queue_warn_depth)
        self._entry_signal_to_submit_warn_ms = max(1, entry_signal_to_submit_warn_ms)
        self._outbox_depth_warn = max(1, outbox_depth_warn)
        self._reconcile_stale_after = timedelta(seconds=max(1, reconcile_stale_after_seconds))
        self._phase = RuntimePhase.CONFIG_LOADING
        self._manual_pause_reason: str | None = None
        self._degraded_reason: str | None = None
        self._db_ready = False
        self._trading_client_ready = False
        self._worker_health: dict[str, WorkerHealth] = {}

    def register_worker(
        self,
        name: str,
        *,
        priority: str,
        state: WorkerLifecycleState = WorkerLifecycleState.STARTING,
        healthy: bool = True,
        detail: str = "",
    ) -> None:
        self._worker_health[name] = WorkerHealth(
            name=name,
            priority=priority,
            state=state,
            healthy=healthy,
            detail=detail,
            last_heartbeat_at=_utc_now(),
        )

    def heartbeat_worker(
        self,
        name: str,
        *,
        state: WorkerLifecycleState = WorkerLifecycleState.RUNNING,
        healthy: bool = True,
        detail: str = "",
        last_error: str | None = None,
    ) -> None:
        current = self._worker_health.get(name)
        priority = current.priority if current is not None else "P2"
        self._worker_health[name] = WorkerHealth(
            name=name,
            priority=priority,
            state=state,
            healthy=healthy,
            detail=detail,
            last_heartbeat_at=_utc_now(),
            last_error=last_error,
        )

    def mark_worker_error(self, name: str, *, detail: str, last_error: str) -> None:
        current = self._worker_health.get(name)
        priority = current.priority if current is not None else "P2"
        self._worker_health[name] = WorkerHealth(
            name=name,
            priority=priority,
            state=WorkerLifecycleState.DEGRADED,
            healthy=False,
            detail=detail,
            last_heartbeat_at=_utc_now(),
            last_error=last_error,
        )

    def set_phase(self, phase: RuntimePhase) -> None:
        self._phase = phase

    def mark_db_ready(self, ready: bool, *, reason: str = "") -> None:
        self._db_ready = ready
        if not ready and reason:
            self._degraded_reason = reason

    def mark_trading_client_ready(self, ready: bool, *, reason: str = "") -> None:
        self._trading_client_ready = ready
        if not ready and reason:
            self._degraded_reason = reason

    def pause_trading(self, reason: str) -> None:
        self._manual_pause_reason = reason or "manual_pause"
        self._phase = RuntimePhase.PAUSED

    def resume_trading(self) -> None:
        self._manual_pause_reason = None
        if self._phase == RuntimePhase.PAUSED:
            self._phase = RuntimePhase.TRADING_ENABLED

    def mark_degraded(self, reason: str) -> None:
        self._degraded_reason = reason or "runtime_degraded"
        self._phase = RuntimePhase.DEGRADED

    def clear_degraded(self) -> None:
        self._degraded_reason = None
        if self._phase == RuntimePhase.DEGRADED:
            self._phase = RuntimePhase.WORKERS_STARTED

    async def refresh(self) -> RuntimeSnapshot:
        queue_depths = _as_mapping(self._event_bus.snapshot())
        metrics = _as_mapping(self._snapshot_from(self._metrics_snapshot_provider))
        overloaded = self._should_pause_low_priority(queue_depths, metrics)
        if overloaded:
            self._event_bus.pause_low_priority()
            if self._degraded_reason is None:
                self._degraded_reason = "p0_backpressure"
            if self._phase == RuntimePhase.TRADING_ENABLED:
                self._phase = RuntimePhase.DEGRADED
        elif self._event_bus.low_priority_paused():
            await self._event_bus.resume_low_priority()
            if self._degraded_reason == "p0_backpressure":
                self._degraded_reason = None
                if self._manual_pause_reason is None:
                    self._phase = RuntimePhase.WORKERS_STARTED
            queue_depths = _as_mapping(self._event_bus.snapshot())

        snapshot = self._snapshot_with(queue_depths=queue_depths, metrics=metrics)
        readiness = snapshot.readiness
        if readiness is not None and readiness.ready and self._manual_pause_reason is None:
            self._phase = RuntimePhase.TRADING_ENABLED
            snapshot = self._snapshot_with(queue_depths=queue_depths, metrics=metrics)
        return snapshot

    def snapshot(self) -> RuntimeSnapshot:
        return self._snapshot_with()

    def _snapshot_with(
        self,
        *,
        queue_depths: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> RuntimeSnapshot:
        settings_readiness = _as_mapping(self._settings_readiness)
        if queue_depths is None:
            queue_depths = _as_mapping(self._event_bus.snapshot())
        scheduler_value = self._snapshot_from(self._scheduler_snapshot_provider)
        scheduler = scheduler_value if isinstance(scheduler_value, SchedulerSnapshot) else None
        account_snapshot = _as_mapping(self._snapshot_from(self._account_snapshot_provider))
        market_ws = _as_mapping(self._snapshot_from(self._market_ws_snapshot_provider))
        user_ws = _as_mapping(self._snapshot_from(self._user_ws_snapshot_provider))
        reconcile = _as_mapping(self._snapshot_from(self._reconcile_snapshot_provider))
        persistence = _as_mapping(self._snapshot_from(self._persistence_snapshot_provider))
        if metrics is None:
            metrics = _as_mapping(self._snapshot_from(self._metrics_snapshot_provider))
        sse_snapshot = _as_mapping(self._snapshot_from(self._sse_snapshot_provider))
        readiness = self._build_readiness(
            settings_readiness=settings_readiness,
            queue_depths=queue_depths,
            account_snapshot=account_snapshot,
            market_ws=market_ws,
            user_ws=user_ws,
            reconcile=reconcile,
            persistence=persistence,
        )
        automatic_trading_enabled = (
            readiness.ready
            and self._phase == RuntimePhase.TRADING_ENABLED
            and self._manual_pause_reason is None
            and self._degraded_reason is None
        )
        return RuntimeSnapshot(
            phase=self._phase,
            automatic_trading_enabled=automatic_trading_enabled,
            low_priority_paused=self._event_bus.low_priority_paused(),
            live=self._phase not in {RuntimePhase.STOPPING, RuntimePhase.STOPPED},
            manual_pause_reason=self._manual_pause_reason,
            degraded_reason=self._degraded_reason,
            readiness=readiness,
            settings_readiness=settings_readiness,
            queue_depths=queue_depths,
            scheduler=scheduler,
            worker_health=tuple(sorted(self._worker_health.values(), key=lambda item: item.name)),
            account=account_snapshot,
            market_ws=market_ws,
            user_ws=user_ws,
            reconcile=reconcile,
            persistence=persistence,
            metrics=metrics,
            sse_active_subscribers=_int(sse_snapshot, "sse_active_subscribers"),
            sse_dropped_events_total=_int(sse_snapshot, "sse_dropped_events_total"),
        )

    def _build_readiness(
        self,
        *,
        settings_readiness: Mapping[str, Any],
        queue_depths: Mapping[str, Any],
        account_snapshot: Mapping[str, Any],
        market_ws: Mapping[str, Any],
        user_ws: Mapping[str, Any],
        reconcile: Mapping[str, Any],
        persistence: Mapping[str, Any],
    ) -> ReadinessSnapshot:
        config_ready = _bool(settings_readiness, "ready_to_trade")
        # MarketWsWorkerStatus.connected 由 ws_loops lifecycle hook 维护，
        # starting 阶段未握手时严格为 False；这里不再 fallback 到 "无 last_error"
        # 推断，否则会复现 N12 的"假性 connected"（CLAUDE.md §10 可审计原因）。
        market_ws_connected = _bool(market_ws, "connected")
        user_ws_connected = _bool(user_ws, "connected", _bool(account_snapshot, "user_ws_connected"))
        last_reconcile_at = _datetime(account_snapshot, "last_reconcile_at") or _datetime(
            reconcile,
            "last_reconcile_at",
        )
        reconcile_fresh = (
            last_reconcile_at is not None and _utc_now() - last_reconcile_at <= self._reconcile_stale_after
        )
        outbox_backlog_ok = _int(persistence, "outbox_depth") < self._outbox_depth_warn

        blocking_reasons: list[str] = []
        if not config_ready:
            blocking_reasons.append("config_not_ready")
        if not self._db_ready:
            blocking_reasons.append("db_not_ready")
        if not self._trading_client_ready:
            blocking_reasons.append("trading_client_not_ready")
        if not market_ws_connected:
            blocking_reasons.append("market_ws_not_connected")
        if not user_ws_connected:
            blocking_reasons.append("user_ws_not_connected")
        if not reconcile_fresh:
            blocking_reasons.append("reconcile_not_fresh")
        if not outbox_backlog_ok:
            blocking_reasons.append("outbox_backlog_high")
        if self._manual_pause_reason:
            blocking_reasons.append(self._manual_pause_reason)
        if self._degraded_reason:
            blocking_reasons.append(self._degraded_reason)
        if self._phase not in {
            RuntimePhase.TRADING_ENABLED,
            RuntimePhase.WORKERS_STARTED,
            RuntimePhase.DEGRADED,
            RuntimePhase.PAUSED,
        }:
            blocking_reasons.append(f"phase={self._phase.value}")

        warnings: list[str] = []
        if _bool(queue_depths, "low_priority_paused"):
            warnings.append("low_priority_paused")
        if settings_readiness.get("warnings"):
            warnings.append("config_warning_present")

        ready = not blocking_reasons and self._phase in {
            RuntimePhase.WORKERS_STARTED,
            RuntimePhase.TRADING_ENABLED,
        }
        automatic_trading_enabled = (
            ready and _bool(account_snapshot, "allow_new_entries", user_ws_connected)
        )
        return ReadinessSnapshot(
            phase=self._phase,
            live=self._phase not in {RuntimePhase.STOPPING, RuntimePhase.STOPPED},
            ready=ready,
            automatic_trading_enabled=automatic_trading_enabled,
            config_ready=config_ready,
            db_ready=self._db_ready,
            trading_client_ready=self._trading_client_ready,
            market_ws_connected=market_ws_connected,
            user_ws_connected=user_ws_connected,
            reconcile_fresh=reconcile_fresh,
            outbox_backlog_ok=outbox_backlog_ok,
            low_priority_paused=self._event_bus.low_priority_paused(),
            blocking_reasons=tuple(blocking_reasons),
            warnings=tuple(warnings),
            last_reconcile_at=last_reconcile_at,
        )

    def _should_pause_low_priority(
        self,
        queue_depths: Mapping[str, Any],
        metrics: Mapping[str, Any],
    ) -> bool:
        trading_depth = _int(queue_depths, "trading_queue_depth")
        if trading_depth >= self._trading_queue_warn_depth:
            return True
        latency_ms = _extract_metric_value(metrics, "entry_signal_to_submit_ms")
        if latency_ms is not None and latency_ms >= self._entry_signal_to_submit_warn_ms:
            return True
        for key in ("trading_lock_wait_ms", "executor_queue_wait_ms"):
            value = _extract_metric_value(metrics, key)
            if value is not None and value >= self._entry_signal_to_submit_warn_ms:
                return True
        return False

    @staticmethod
    def _snapshot_from(provider: SnapshotProvider | None) -> Any:
        if provider is None:
            return None
        return provider()


def _extract_metric_value(metrics: Mapping[str, Any], metric_name: str) -> int | None:
    def _to_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    if metric_name in metrics:
        value = metrics.get(metric_name)
        return _to_int(value)
    gauges = metrics.get("gauges")
    if isinstance(gauges, Mapping):
        gauge = gauges.get(metric_name)
        if isinstance(gauge, Mapping):
            value = gauge.get("value")
        else:
            value = gauge
        return _to_int(value)
    if isinstance(gauges, list):
        for gauge in gauges:
            if not isinstance(gauge, Mapping) or gauge.get("name") != metric_name:
                continue
            return _to_int(gauge.get("value"))
    return None
