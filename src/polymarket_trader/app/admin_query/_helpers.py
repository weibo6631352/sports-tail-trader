"""Admin 查询 mixin 共享的纯函数工具。

这些 helper 不依赖宿主状态，只做时间戳解析、百分位计算、latency 投影、
决策记录序列化等纯计算。集中在这里避免子 mixin 互相循环依赖。
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
    """线性插值的百分位数；空样本返回 None。

    避免引入 numpy 依赖；纯 Python 实现，按 PostgreSQL ``percentile_cont`` 同义。
    """

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


def _empty_latency_payload(
    event_types: tuple[str, ...],
    sample_limit: int,
    window_ms: int | None,
) -> dict[str, Any]:
    return {
        "window_ms": window_ms,
        "sample_limit": sample_limit,
        "event_types": list(event_types),
        "sample_count": 0,
        "stages": {
            stage_name: {"count": 0, "percentiles_ms": {}, "max_ms": None, "min_ms": None}
            for stage_name, _, _ in _LATENCY_STAGES
        },
    }


def _compute_latency_payload(
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

    stage_payload: dict[str, Any] = {}
    for stage_name in stages:
        values = sorted(stages[stage_name])
        percentile_map = {
            f"p{int(q * 100)}": _percentile(values, q) for q in _LATENCY_PERCENTILES
        }
        stage_payload[stage_name] = {
            "count": len(values),
            "percentiles_ms": percentile_map,
            "max_ms": values[-1] if values else None,
            "min_ms": values[0] if values else None,
        }
    return {
        "window_ms": window_ms,
        "sample_limit": sample_limit,
        "event_types": list(event_types),
        "sample_count": len(events),
        "stages": stage_payload,
    }


def _decision_record_payload(record: DecisionRecord) -> dict[str, Any]:
    """决策录制行的 admin 视图——保持字段命名与 DB 列对齐。"""

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
