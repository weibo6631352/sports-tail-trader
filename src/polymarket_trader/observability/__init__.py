"""【横切】observability —— metrics + trace + cpu_track + health。

原架构方案 §11 横切关注点。本包提供：

# 模块

- `metrics` —— `MetricsRegistry`：内存 counter / gauge / histogram，O(1) 写
  （CLAUDE.md §7 P0 不阻塞）。Snapshot DTO 含 GaugeSnapshot / CounterSnapshot /
  HistogramSnapshot / ReconcileSnapshot / TradingGateSnapshot 等
- `trace` —— `new_trace_id` / `current_trace_id` / `bind_trace_id` / `ensure_trace_id`
  / `trace_scope`：trace_id 贯穿事件 → 决策 → 订单 → fill → audit
- `cpu_track` —— `cpu_track` decorator + `step_track` ctx-mgr：
  双维度（CPU + wall）耗时记录，async/sync 通吃，异常吞掉永不影响业务路径
  （§11.1 P0 sync-collect + async-report 已实现，无需独立 perf_recorder）
- `health` —— `HealthReporter`：`/health` 端点数据源（§11.4），5 个维度
  （overall / live_sources / ws / decision / account），机器可读 `status`
  字段供外部监控直接告警

# 设计原则（§11.1）

- P0 路径 sync-collect + async-report：所有 metric 收集纯内存计数 / 时间戳，
  聚合 + 上报走 audit outbox 异步消费
- 必须 actionable：每个 metric 答得出"指标变差时该做什么"
- trace_id 贯穿全链路：事件 → 决策 → 订单 → fill → audit 用同一 trace_id
- 运行时快照默认暴露：每个 worker / store 必须实现 `snapshot()`

# 禁止（§11.5）

- ❌ P0 路径 `await` metrics 上报
- ❌ metrics label 用高基数字段（condition_id / trace_id / token_id）
- ❌ 为单次 debug 加 metric 不删（debug 用 audit_event 临时打点）
- ❌ 把 audit_event 当 metric 用（audit 频率低 + 落 DB，metrics 高频 + 内存）
"""

from .cpu_track import CpuStopwatch, cpu_track, step_track, track_async
from .health import HealthReport, HealthReporter, HealthStatus
from .metrics import MetricsRegistry, MetricsSnapshot
from .prometheus_exporter import render_prometheus
from .trace import bind_trace_id, current_trace_id, ensure_trace_id, new_trace_id, trace_scope

__all__ = [
    "CpuStopwatch",
    "HealthReport",
    "HealthReporter",
    "HealthStatus",
    "MetricsRegistry",
    "MetricsSnapshot",
    "bind_trace_id",
    "cpu_track",
    "current_trace_id",
    "ensure_trace_id",
    "new_trace_id",
    "render_prometheus",
    "step_track",
    "trace_scope",
    "track_async",
]
