"""【app/audit】Persistence worker + 写入侧 dedupe + garbage 过滤。

原架构方案 §13 数据库与审计落盘优化。

# 数据流

```
OutboxEvent batch (来自 event_bus → outbox)
    ↓ PersistenceWorker.run_once 拿 batch
GarbageFilter.filter()    ← §13.2 永不写
    ↓ (留下"值得写"的)
AuditDeduper.filter()     ← §13.3 短窗口去重
    ↓ (留下"首次写"的)
PersistenceWorker._persist_batch
    ↓
PersistenceRepository (PostgreSQL)
```

PersistenceWorker 主动注入 GarbageFilter + AuditDeduper（main.py wire），
两者都是无状态可独立测试。
"""

from __future__ import annotations

from .deduper import AuditDeduper, DedupeStats
from .garbage_filter import GarbageFilter, GarbageFilterStats
from .records import (
    PersistencePlannedRecord,
    PersistenceRecordBuilder,
    route_key,
)
from .worker import (
    PersistenceOutbox,
    PersistenceRepository,
    PersistenceWorker,
    PersistenceWorkerResult,
    PersistenceWorkerSnapshot,
)

__all__ = [
    "AuditDeduper",
    "DedupeStats",
    "GarbageFilter",
    "GarbageFilterStats",
    "PersistenceOutbox",
    "PersistencePlannedRecord",
    "PersistenceRecordBuilder",
    "PersistenceRepository",
    "PersistenceWorker",
    "PersistenceWorkerResult",
    "PersistenceWorkerSnapshot",
    "route_key",
]
