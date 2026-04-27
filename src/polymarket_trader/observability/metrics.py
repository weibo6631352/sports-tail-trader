from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import inf
from threading import RLock
from typing import Any, Mapping

from polymarket_trader.serialization import jsonable

DEFAULT_LATENCY_BUCKETS_MS: tuple[float, ...] = (
    1.0,
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1_000.0,
    2_500.0,
    5_000.0,
    10_000.0,
    inf,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_datetime(value: datetime | None = None) -> datetime:
    value = value or _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_labels(labels: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def _metric_key(name: str, labels: Mapping[str, Any] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
    return (str(name), _normalize_labels(labels))


class JsonSerializable:
    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class GaugeSnapshot(JsonSerializable):
    name: str
    value: float
    labels: tuple[tuple[str, str], ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CounterSnapshot(JsonSerializable):
    name: str
    value: float
    labels: tuple[tuple[str, str], ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class HistogramBucketSnapshot(JsonSerializable):
    upper_bound_ms: float | None
    count: int


@dataclass(frozen=True, slots=True)
class HistogramSnapshot(JsonSerializable):
    name: str
    labels: tuple[tuple[str, str], ...]
    count: int
    sum_ms: float
    min_ms: float | None
    max_ms: float | None
    mean_ms: float | None
    last_ms: float | None
    buckets: tuple[HistogramBucketSnapshot, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class QueueDepthSnapshot(JsonSerializable):
    name: str
    depth: int
    capacity: int
    retained_depth: int
    paused: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WebSocketStateSnapshot(JsonSerializable):
    name: str
    connected: bool
    subscribed_count: int
    last_message_at: datetime | None
    last_message_lag_ms: float | None
    last_event_type: str | None
    last_error: str | None
    reconnect_attempts: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcileSnapshot(JsonSerializable):
    last_started_at: datetime | None
    last_completed_at: datetime | None
    last_duration_ms: float | None
    last_status: str | None
    last_error: str | None
    last_trace_id: str | None
    last_actions: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TradingGateSnapshot(JsonSerializable):
    enabled: bool
    reason: str | None
    source: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TimestampSnapshot(JsonSerializable):
    name: str
    at: datetime
    age_ms: float


@dataclass(frozen=True, slots=True)
class MetricsSnapshot(JsonSerializable):
    collected_at: datetime
    queue_depths: tuple[QueueDepthSnapshot, ...]
    ws_states: tuple[WebSocketStateSnapshot, ...]
    reconcile: ReconcileSnapshot
    trading_gate: TradingGateSnapshot
    counters: tuple[CounterSnapshot, ...]
    gauges: tuple[GaugeSnapshot, ...]
    histograms: tuple[HistogramSnapshot, ...]
    timestamps: tuple[TimestampSnapshot, ...]


class MetricsRegistry:
    """In-memory metrics registry for hot-path snapshots."""

    def __init__(self, *, latency_buckets_ms: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS_MS) -> None:
        self._latency_buckets_ms = tuple(latency_buckets_ms)
        self._lock = RLock()
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], GaugeSnapshot] = {}
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], CounterSnapshot] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self._queue_depths: dict[str, QueueDepthSnapshot] = {}
        self._ws_states: dict[str, WebSocketStateSnapshot] = {}
        self._timestamps: dict[str, datetime] = {}
        self._reconcile = ReconcileSnapshot(
            last_started_at=None,
            last_completed_at=None,
            last_duration_ms=None,
            last_status=None,
            last_error=None,
            last_trace_id=None,
            last_actions=0,
            updated_at=_utc_now(),
        )
        self._trading_gate = TradingGateSnapshot(
            enabled=False,
            reason=None,
            source=None,
            updated_at=_utc_now(),
        )

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> GaugeSnapshot:
        key = _metric_key(name, labels)
        snapshot = GaugeSnapshot(
            name=key[0],
            value=float(value),
            labels=key[1],
            updated_at=_normalize_datetime(updated_at),
        )
        with self._lock:
            self._gauges[key] = snapshot
        return snapshot

    def inc_counter(
        self,
        name: str,
        amount: float = 1.0,
        *,
        labels: Mapping[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> CounterSnapshot:
        key = _metric_key(name, labels)
        with self._lock:
            current = self._counters.get(key)
            snapshot = CounterSnapshot(
                name=key[0],
                value=(0.0 if current is None else current.value) + float(amount),
                labels=key[1],
                updated_at=_normalize_datetime(updated_at),
            )
            self._counters[key] = snapshot
        return snapshot

    def observe_latency(
        self,
        name: str,
        value_ms: float,
        *,
        labels: Mapping[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> HistogramSnapshot:
        key = _metric_key(name, labels)
        with self._lock:
            data = self._histograms.setdefault(key, _empty_histogram_data(key[0], key[1], self._latency_buckets_ms))
            _observe_histogram_data(data, value_ms, updated_at=updated_at)
            return _histogram_snapshot(data)

    def set_queue_depth(
        self,
        name: str,
        depth: int,
        *,
        capacity: int = 0,
        retained_depth: int = 0,
        paused: bool = False,
        updated_at: datetime | None = None,
    ) -> QueueDepthSnapshot:
        snapshot = QueueDepthSnapshot(
            name=str(name),
            depth=max(0, int(depth)),
            capacity=max(0, int(capacity)),
            retained_depth=max(0, int(retained_depth)),
            paused=bool(paused),
            updated_at=_normalize_datetime(updated_at),
        )
        with self._lock:
            self._queue_depths[snapshot.name] = snapshot
        return snapshot

    def set_ws_state(
        self,
        name: str,
        *,
        connected: bool,
        subscribed_count: int = 0,
        last_message_at: datetime | None = None,
        last_message_lag_ms: float | None = None,
        last_event_type: str | None = None,
        last_error: str | None = None,
        reconnect_attempts: int = 0,
        updated_at: datetime | None = None,
    ) -> WebSocketStateSnapshot:
        snapshot = WebSocketStateSnapshot(
            name=str(name),
            connected=bool(connected),
            subscribed_count=max(0, int(subscribed_count)),
            last_message_at=_normalize_datetime(last_message_at) if last_message_at else None,
            last_message_lag_ms=None if last_message_lag_ms is None else float(last_message_lag_ms),
            last_event_type=last_event_type,
            last_error=last_error,
            reconnect_attempts=max(0, int(reconnect_attempts)),
            updated_at=_normalize_datetime(updated_at),
        )
        with self._lock:
            self._ws_states[snapshot.name] = snapshot
        return snapshot

    def mark_timestamp(self, name: str, *, at: datetime | None = None) -> TimestampSnapshot:
        at = _normalize_datetime(at)
        with self._lock:
            self._timestamps[str(name)] = at
        return TimestampSnapshot(name=str(name), at=at, age_ms=0.0)

    def record_reconcile(
        self,
        duration_ms: float | None,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        status: str | None = None,
        error: str | None = None,
        trace_id: str | None = None,
        actions: int = 0,
    ) -> ReconcileSnapshot:
        completed_at = _normalize_datetime(completed_at)
        started_at = _normalize_datetime(started_at) if started_at is not None else None
        snapshot = ReconcileSnapshot(
            last_started_at=started_at,
            last_completed_at=completed_at,
            last_duration_ms=None if duration_ms is None else float(duration_ms),
            last_status=status,
            last_error=error,
            last_trace_id=trace_id,
            last_actions=max(0, int(actions)),
            updated_at=completed_at,
        )
        with self._lock:
            self._reconcile = snapshot
            if duration_ms is not None:
                key = _metric_key("reconcile_duration_ms", None)
                data = self._histograms.setdefault(
                    key,
                    _empty_histogram_data(key[0], key[1], self._latency_buckets_ms),
                )
                _observe_histogram_data(data, duration_ms, updated_at=completed_at)
        return snapshot

    def set_trading_gate(
        self,
        enabled: bool,
        *,
        reason: str | None = None,
        source: str | None = None,
        updated_at: datetime | None = None,
    ) -> TradingGateSnapshot:
        snapshot = TradingGateSnapshot(
            enabled=bool(enabled),
            reason=reason,
            source=source,
            updated_at=_normalize_datetime(updated_at),
        )
        with self._lock:
            self._trading_gate = snapshot
        return snapshot

    def queue_depth(self, name: str) -> QueueDepthSnapshot | None:
        with self._lock:
            return self._queue_depths.get(name)

    def ws_state(self, name: str) -> WebSocketStateSnapshot | None:
        with self._lock:
            return self._ws_states.get(name)

    def reconcile_snapshot(self) -> ReconcileSnapshot:
        with self._lock:
            return self._reconcile

    def trading_gate_snapshot(self) -> TradingGateSnapshot:
        with self._lock:
            return self._trading_gate

    def snapshot(self) -> MetricsSnapshot:
        collected_at = _utc_now()
        with self._lock:
            return MetricsSnapshot(
                collected_at=collected_at,
                queue_depths=tuple(sorted(self._queue_depths.values(), key=lambda item: item.name)),
                ws_states=tuple(sorted(self._ws_states.values(), key=lambda item: item.name)),
                reconcile=self._reconcile,
                trading_gate=self._trading_gate,
                counters=tuple(sorted(self._counters.values(), key=lambda item: (item.name, item.labels))),
                gauges=tuple(sorted(self._gauges.values(), key=lambda item: (item.name, item.labels))),
                histograms=tuple(
                    _histogram_snapshot(data)
                    for _, data in sorted(self._histograms.items(), key=lambda item: item[0])
                ),
                timestamps=tuple(
                    TimestampSnapshot(
                        name=name,
                        at=at,
                        age_ms=max(0.0, (collected_at - at).total_seconds() * 1000.0),
                    )
                    for name, at in sorted(self._timestamps.items())
                ),
            )


def _empty_histogram_data(
    name: str,
    labels: tuple[tuple[str, str], ...],
    buckets_ms: tuple[float, ...],
) -> dict[str, Any]:
    return {
        "name": name,
        "labels": labels,
        "buckets_ms": buckets_ms,
        "bucket_counts": [0 for _ in buckets_ms],
        "count": 0,
        "sum_ms": 0.0,
        "min_ms": None,
        "max_ms": None,
        "last_ms": None,
        "updated_at": _utc_now(),
    }


def _observe_histogram_data(
    data: dict[str, Any],
    value_ms: float,
    *,
    updated_at: datetime | None = None,
) -> None:
    value = max(0.0, float(value_ms))
    data["count"] = int(data["count"]) + 1
    data["sum_ms"] = float(data["sum_ms"]) + value
    data["last_ms"] = value
    data["min_ms"] = value if data["min_ms"] is None else min(float(data["min_ms"]), value)
    data["max_ms"] = value if data["max_ms"] is None else max(float(data["max_ms"]), value)
    data["updated_at"] = _normalize_datetime(updated_at)
    bucket_counts = data["bucket_counts"]
    for index, upper_bound in enumerate(data["buckets_ms"]):
        if value <= upper_bound:
            bucket_counts[index] += 1
            break


def _histogram_snapshot(data: Mapping[str, Any]) -> HistogramSnapshot:
    count = int(data["count"])
    sum_ms = float(data["sum_ms"])
    buckets = tuple(
        HistogramBucketSnapshot(
            upper_bound_ms=None if upper_bound == inf else float(upper_bound),
            count=int(bucket_count),
        )
        for upper_bound, bucket_count in zip(data["buckets_ms"], data["bucket_counts"], strict=False)
    )
    return HistogramSnapshot(
        name=str(data["name"]),
        labels=tuple(data["labels"]),
        count=count,
        sum_ms=sum_ms,
        min_ms=data["min_ms"],
        max_ms=data["max_ms"],
        mean_ms=(sum_ms / count) if count else None,
        last_ms=data["last_ms"],
        buckets=buckets,
        updated_at=data["updated_at"],
    )


__all__ = [
    "CounterSnapshot",
    "DEFAULT_LATENCY_BUCKETS_MS",
    "GaugeSnapshot",
    "HistogramBucketSnapshot",
    "HistogramSnapshot",
    "JsonSerializable",
    "MetricsRegistry",
    "MetricsSnapshot",
    "QueueDepthSnapshot",
    "ReconcileSnapshot",
    "TimestampSnapshot",
    "TradingGateSnapshot",
    "WebSocketStateSnapshot",
]
