from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.serialization import jsonable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimePhase(StrEnum):
    CONFIG_LOADING = "config_loading"
    INFRA_READY = "infra_ready"
    RECOVERING_SNAPSHOT = "recovering_snapshot"
    RECONCILING = "reconciling"
    WORKERS_STARTED = "workers_started"
    TRADING_ENABLED = "trading_enabled"
    PAUSED = "paused"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"


class WorkerLifecycleState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    DEGRADED = "degraded"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    name: str
    priority: str
    state: WorkerLifecycleState
    healthy: bool
    detail: str = ""
    last_heartbeat_at: datetime | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class SchedulerJob:
    name: str
    priority: str
    interval_seconds: float | None
    enabled: bool
    paused: bool
    running: bool
    run_count: int
    tags: tuple[str, ...] = ()
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    # 直接由 scheduler 在每次运行结束时计算 finished-started，避免外部观测者用
    # "上一轮 finished - 当前轮 started" 算出负值（N9）。
    last_duration_ms: float | None = None
    next_run_at: datetime | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    jobs: tuple[SchedulerJob, ...]
    created_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    phase: RuntimePhase
    live: bool
    ready: bool
    automatic_trading_enabled: bool
    config_ready: bool
    db_ready: bool
    trading_client_ready: bool
    market_ws_connected: bool
    user_ws_connected: bool
    reconcile_fresh: bool
    outbox_backlog_ok: bool
    low_priority_paused: bool
    blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    last_reconcile_at: datetime | None = None
    created_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    phase: RuntimePhase
    automatic_trading_enabled: bool
    low_priority_paused: bool
    live: bool
    manual_pause_reason: str | None = None
    degraded_reason: str | None = None
    readiness: ReadinessSnapshot | None = None
    settings_readiness: Mapping[str, Any] | None = None
    queue_depths: Mapping[str, Any] | None = None
    scheduler: SchedulerSnapshot | None = None
    worker_health: tuple[WorkerHealth, ...] = ()
    account: Mapping[str, Any] | None = None
    market_ws: Mapping[str, Any] | None = None
    user_ws: Mapping[str, Any] | None = None
    reconcile: Mapping[str, Any] | None = None
    persistence: Mapping[str, Any] | None = None
    metrics: Mapping[str, Any] | None = None
    sse_active_subscribers: int = 0
    sse_dropped_events_total: int = 0
    created_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


def trading_gate_reason(snapshot: RuntimeSnapshot) -> str | None:
    if snapshot.automatic_trading_enabled:
        return None
    if snapshot.manual_pause_reason:
        return snapshot.manual_pause_reason
    if snapshot.degraded_reason:
        return snapshot.degraded_reason
    if snapshot.readiness is not None and snapshot.readiness.blocking_reasons:
        return snapshot.readiness.blocking_reasons[0]
    return snapshot.phase.value
