"""ObservabilityBridge —— 周期把各组件 stats 投影到 MetricsRegistry。

原架构方案 §11.2 关键指标表的批量实现路径。各业务组件保持解耦
（不直接依赖 MetricsRegistry），bridge 在 scheduler 周期 job 内集中读取
组件 snapshot/stats → 写入 metric gauge。

# 优点

- **业务组件保持纯净**：AuditDeduper / GarbageFilter / LiveStateStore 等不
  需要持有 MetricsRegistry 引用，仍可独立测试
- **集中维护**：所有 metric 名 + 阈值 + label 在本模块统一管理，改 metric
  schema 只动一处
- **低成本**：5s 周期同步几个 dict / counter 数值，零业务路径开销
- **可扩展**：新增组件按 register 注入 + 加 `_sync_xxx` 方法即可

# 当前覆盖

| 维度 | 来源 | metric |
|---|---|---|
| audit dedupe | AuditDeduper.stats | audit_dedupe_total / audit_dedupe_dropped / audit_dedupe_drop_by_type{event_type} |
| audit garbage | GarbageFilter.stats | audit_garbage_total / audit_garbage_dropped / audit_garbage_drop_by_reason{reason} |
| live source | LiveStateStore + LiveSourceRegistry | live_source_bucket_events / live_source_bucket_stale_s / live_source_subscribers (gauge by source) |
| account | AccountStateStore.snapshot | account_balance_usdc / account_position_count |

# 用法

main.py 构造 bridge + scheduler 注册 5s job：

```python
bridge = ObservabilityBridge(
    metrics=runtime.metrics,
    audit_deduper=runtime.audit_deduper,
    garbage_filter=runtime.garbage_filter,
    live_state_store=runtime.live_state_store,
    live_source_registry=runtime.live_source_registry,
    account_state_store=runtime.account_state_store,
)
runtime.scheduler.register_job("observability_bridge_sync", bridge.sync, interval_seconds=5.0)
```
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_trader.app.audit.deduper import AuditDeduper
    from polymarket_trader.app.audit.garbage_filter import GarbageFilter
    from polymarket_trader.observability.metrics import MetricsRegistry
    from polymarket_trader.pipeline.ingest.live_source import LiveSourceRegistry, LiveStateStore
    from polymarket_trader.runtime.account_state import AccountStateStore

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ObservabilityBridge:
    def __init__(
        self,
        *,
        metrics: "MetricsRegistry",
        audit_deduper: "AuditDeduper | None" = None,
        garbage_filter: "GarbageFilter | None" = None,
        live_state_store: "LiveStateStore | None" = None,
        live_source_registry: "LiveSourceRegistry | None" = None,
        account_state_store: "AccountStateStore | None" = None,
    ) -> None:
        self._metrics = metrics
        self._audit_deduper = audit_deduper
        self._garbage_filter = garbage_filter
        self._live_state_store = live_state_store
        self._live_source_registry = live_source_registry
        self._account_state_store = account_state_store

    async def sync(self) -> None:
        """scheduler 周期调用——sync 所有组件状态到 metrics。

        每段独立 try/except，一个组件失败不阻断其他维度上报。
        """

        self._safe(self._sync_audit_dedupe, "audit_dedupe")
        self._safe(self._sync_audit_garbage, "audit_garbage")
        self._safe(self._sync_live_sources, "live_sources")
        self._safe(self._sync_account, "account")

    def _safe(self, fn, name: str) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.exception("observability_bridge sync segment failed: %s", name)

    def _sync_audit_dedupe(self) -> None:
        if self._audit_deduper is None:
            return
        stats = self._audit_deduper.stats()
        self._metrics.set_gauge("audit_dedupe_total", float(stats.total))
        self._metrics.set_gauge("audit_dedupe_inserted", float(stats.inserted))
        self._metrics.set_gauge("audit_dedupe_updated", float(stats.updated))
        for event_type, count in stats.by_event_type_updated.items():
            self._metrics.set_gauge(
                "audit_dedupe_update_by_type",
                float(count),
                labels={"event_type": event_type},
            )

    def _sync_audit_garbage(self) -> None:
        if self._garbage_filter is None:
            return
        stats = self._garbage_filter.stats()
        self._metrics.set_gauge("audit_garbage_total", float(stats.total))
        self._metrics.set_gauge("audit_garbage_dropped", float(stats.dropped))
        for reason, count in stats.by_reason_dropped.items():
            self._metrics.set_gauge(
                "audit_garbage_drop_by_reason",
                float(count),
                labels={"reason": reason},
            )

    def _sync_live_sources(self) -> None:
        if self._live_state_store is None or self._live_source_registry is None:
            return
        now = _utc_now()
        for bucket in self._live_state_store.all_buckets():
            labels = {"source": bucket.source.as_label()}
            self._metrics.set_gauge(
                "live_source_bucket_events",
                float(len(bucket.events)),
                labels=labels,
            )
            stale_s = (now - bucket.observed_at).total_seconds()
            self._metrics.set_gauge(
                "live_source_bucket_stale_s",
                float(stale_s),
                labels=labels,
            )
            sub_count = len(self._live_source_registry.subscribers_for(bucket.source))
            self._metrics.set_gauge(
                "live_source_subscribers",
                float(sub_count),
                labels=labels,
            )

    def _sync_account(self) -> None:
        if self._account_state_store is None:
            return
        snap = self._account_state_store.snapshot()
        self._metrics.set_gauge("account_balance_usdc", float(snap.balance_usdc))
        self._metrics.set_gauge(
            "account_allowance_usdc", float(snap.allowance_usdc)
        )
        # 仅统计有 shares 的活跃持仓
        active = sum(1 for p in snap.positions if p.shares > 0)
        self._metrics.set_gauge("account_position_count", float(active))
        self._metrics.set_gauge(
            "account_open_orders_count", float(len(snap.open_orders))
        )
