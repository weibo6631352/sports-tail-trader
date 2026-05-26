"""Aggregator-shared pure-function helpers.

迁自 `app/admin_query/_helpers.py`（admin_query 目录已删除）。
- decision record 投影
- latency 分位数计算（用于 AnalyticsAggregator.latency_percentiles_snapshot）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from polymarket_trader.app.admin_serialization import jsonable
from polymarket_trader.domain.decisions import DecisionRecord


_LATENCY_STAGES: tuple[tuple[str, str, str], ...] = (
    ("queue_to_sign", "queued_at", "signed_at"),
    ("sign_to_submit", "sign_started_at", "submitted_at"),
    ("submit_to_ack", "submitted_at", "ack_at"),
    ("queue_to_ack", "queued_at", "ack_at"),
)

_LATENCY_PERCENTILES: tuple[float, ...] = (0.5, 0.9, 0.95, 0.99)


def _parse_iso(ts: Any) -> datetime | None:
    if ts is None or not isinstance(ts, str):
        return None
    text = ts.strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if q <= 0:
        return values[0]
    if q >= 1:
        return values[-1]
    pos = q * (len(values) - 1)
    lower_idx = int(pos)
    frac = pos - lower_idx
    if lower_idx + 1 >= len(values):
        return values[lower_idx]
    return values[lower_idx] + frac * (values[lower_idx + 1] - values[lower_idx])


def empty_latency_payload(
    event_types: tuple[str, ...],
    sample_limit: int,
    window_ms: int | None,
) -> dict[str, Any]:
    return {
        "window_ms": window_ms,
        "sample_limit": sample_limit,
        "event_types": list(event_types),
        "sample_count": 0,
        "stages": [
            {
                "stage": stage_name,
                "sample_count": 0,
                "p50_ms": None,
                "p90_ms": None,
                "p95_ms": None,
                "p99_ms": None,
                "min_ms": None,
                "max_ms": None,
            }
            for stage_name, _, _ in _LATENCY_STAGES
        ],
    }


def compute_latency_payload(
    events: tuple[Any, ...],
    event_types: tuple[str, ...],
    sample_limit: int,
    window_ms: int | None,
) -> dict[str, Any]:
    stages: dict[str, list[float]] = {name: [] for name, _, _ in _LATENCY_STAGES}
    for event in events:
        payload = event.payload or {}
        timestamps = payload.get("timestamps") if isinstance(payload, dict) else None
        if not isinstance(timestamps, dict):
            continue
        parsed: dict[str, datetime | None] = {
            key: _parse_iso(timestamps.get(key))
            for key in ("queued_at", "sign_started_at", "signed_at", "submitted_at", "ack_at")
        }
        for stage_name, start_key, end_key in _LATENCY_STAGES:
            start = parsed.get(start_key)
            end = parsed.get(end_key)
            if start is None or end is None:
                continue
            delta_ms = (end - start).total_seconds() * 1000.0
            if delta_ms < 0:
                continue
            stages[stage_name].append(delta_ms)

    stage_list: list[dict[str, Any]] = []
    for stage_name in stages:
        values = sorted(stages[stage_name])
        pct = {q: _percentile(values, q) for q in _LATENCY_PERCENTILES}
        stage_list.append(
            {
                "stage": stage_name,
                "sample_count": len(values),
                "p50_ms": pct.get(0.50),
                "p90_ms": pct.get(0.90),
                "p95_ms": pct.get(0.95),
                "p99_ms": pct.get(0.99),
                "min_ms": values[0] if values else None,
                "max_ms": values[-1] if values else None,
            }
        )
    return {
        "window_ms": window_ms,
        "sample_limit": sample_limit,
        "event_types": list(event_types),
        "sample_count": len(events),
        "stages": stage_list,
    }


def decision_record_payload(record: DecisionRecord) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "trace_id": record.trace_id,
        "hook_name": record.hook_name or None,
        "condition_id": record.condition_id,
        "token_id": record.token_id,
        "market_slug": record.market_slug,
        "decision_input": dict(record.decision_input),
        "decision_output": dict(record.decision_output),
        "accepted": record.accepted,
        "reason": record.reason,
        "created_at": jsonable(record.created_at),
    }
