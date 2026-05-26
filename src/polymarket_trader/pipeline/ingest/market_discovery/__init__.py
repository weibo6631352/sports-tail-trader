"""市场发现链路 —— gamma /events 拉取 + MarketIngestService + 周期扫描。

# 数据流

```
discovery_runner.run_market_discovery_scan (2s 周期)
    │ 调 gamma /events?live=true
    ↓
ingest_service.MarketIngestService.upsert_market
    │ workflow.select_market (workflow 决定是否纳入 universe)
    ↓
market_registry.upsert
    ↓ (首次 cid → lifecycle_registry.emit_added)
LiveSourceLifecycleBinder.on_market_added → 立即订阅直播源 (C 模式 §3a)
```

# 模块组成

- `ingest_service` —— `MarketIngestService` / `MarketDiscoveryOutcome`：把 gamma
  payload 走 select_market → registry.upsert 的标准化入口
- `discovery_worker` —— `MarketDiscoveryWorker`：消费 outbox 的市场发现事件
  worker（背景常驻）
- `discovery_runner` —— `run_market_discovery_scan`：scheduler 周期调用的扫描器
  + `FullMarketDiscoveryState` 状态机 + `DiscoveryQuery` DTO
"""

from .discovery_runner import (
    DEFAULT_DISCOVERY_QUERY,
    MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS,
    MARKET_DISCOVERY_TICK_SECONDS,
    DiscoveryQuery,
    FullMarketDiscoveryState,
    expand_live_event_market_discovery,
    fetch_full_market_discovery_page,
    refresh_priority_condition_ids,
    run_market_discovery_scan,
)
from .discovery_worker import MarketDiscoveryWorker
from .ingest_service import MarketDiscoveryOutcome, MarketIngestService

__all__ = [
    "DEFAULT_DISCOVERY_QUERY",
    "DiscoveryQuery",
    "FullMarketDiscoveryState",
    "MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS",
    "MARKET_DISCOVERY_TICK_SECONDS",
    "MarketDiscoveryOutcome",
    "MarketDiscoveryWorker",
    "MarketIngestService",
    "expand_live_event_market_discovery",
    "fetch_full_market_discovery_page",
    "refresh_priority_condition_ids",
    "run_market_discovery_scan",
]
