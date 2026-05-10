"""framework 提供的标准 telemetry 事件枚举。

策略调用 ``ports.telemetry.record_event`` 时优先使用这些枚举名，让 admin / 监控
按统一字段聚合。也允许传字符串（向后兼容 / 策略私有事件），但 framework 的
内置 dashboard 只识别枚举值。
"""

from __future__ import annotations

from enum import StrEnum


class TelemetryEvent(StrEnum):
    """framework 知道并按统一维度聚合的 telemetry 事件名。"""

    DECISION_PRODUCED = "decision.produced"
    DECISION_REJECTED = "decision.rejected"
    SCALE_IN_DETECTED = "decision.scale_in_detected"
    RECOVERY_TRIGGERED = "recovery.triggered"
    FILTER_REJECTED = "filter.rejected"
    CONFIG_RELOADED = "config.reloaded"
    HEALTH_TICK_FAILED = "health.tick_failed"
    LIVE_STATE_UPDATED = "live_state.updated"
    CANDIDATE_FUNNEL = "candidate.funnel"
