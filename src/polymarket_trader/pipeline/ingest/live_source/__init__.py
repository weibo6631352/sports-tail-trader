"""直播源订阅 / 拉取 / 匹配 / 校准 7 件套（原架构方案 §3.2-3.3）。

# 模块组成

- `source` —— `LiveSourceKey` + `LiveSourceProvider` enum + 优先级 + 覆盖运动
- `store` —— `LiveStateStore` 按 source 分桶 + refresh listener
- `registry` —— `LiveSourceRegistry` 引用计数订阅 + active sport demand
- `subscription_policy` —— `SportsSubscriptionPolicy` 每 market 订哪个 source
- `feeder` —— `LiveSourceFeeder` per-provider snapshot 拉取 + bucket 分发
- `matcher` —— `LiveSourceMatcher` market ↔ event 候选选择（wrap match hook）
- `calibrator` —— `LiveSourceCalibrator` team/time/league 三角验证
- `match_service` —— `LiveStateMatchService` 编排 matcher + calibrator + 写
  store + 发 ENTRY_SIGNAL_TRIGGERED

# 数据流

```
LiveSourceFeeder (×2: inplay + livescore)
    │ 周期调 client.list_events() → 按 sport 分组
    ↓
LiveStateStore.update(source, events)
    │ 同步 fan-out refresh_listener
    ↓
LiveStateMatchService._on_source_refreshed(source)
    │ subscribers_for(source) → 每 market 跑
    ↓
LiveSourceMatcher.match() → LiveStateMatch
    ↓
LiveSourceCalibrator.calibrate() → CalibrationResult
    ↓
通过 → MarketMetadataStore.upsert + publish ENTRY_SIGNAL_TRIGGERED
失败 → publish SPORTS_LIVE_MATCH_GAP_RECORDED (audit only)
```

# 订阅生命周期

```
market 进入 universe (LifecycleRegistry.emit_added)
    ↓
SportsSubscriptionPolicy.required_sources(market) → [LiveSourceKey, ...]
    ↓
LiveSourceRegistry.subscribe(source, condition_id) × N
    ↓
LiveSourceRegistry.active_sports_for(provider) 变化
    ↓
goalserve client 内部 active_sports_provider 重新评估 → 启动该 sport 的 polling

# 反向：market prune (LifecycleRegistry.emit_pruned)
    ↓
LiveSourceRegistry.unsubscribe_all(condition_id)
    ↓
末个 subscriber 退订时 → 对应 LiveSourceKey 移出 active 集
    ↓
goalserve client 内部该 sport 的 task 在下一轮 demand 评估时停轮询
```
"""

from .calibrator import CalibrationResult, LiveSourceCalibrator
from .default_predicates import is_market_live_active
from .feeder import (
    ExpectedSportsProvider,
    LiveSourceFeeder,
    LiveSourceFeederConfig,
    SnapshotFetcher,
    SportNormalizer,
)
from .lifecycle_binder import LiveSourceLifecycleBinder
from .match_service import LiveStateMatchService
from .matcher import LiveSourceMatcher, MatchHook
from .registry import LiveSourceRegistry
from .source import (
    INPLAY_COVERED_SPORTS,
    LiveSourceKey,
    LiveSourceProvider,
    provider_priority,
    source_priority,
)
from .store import LiveSourceBucket, LiveStateStore, RefreshListener
from .subscription_policy import ActivePredicate, SportResolver, SportsSubscriptionPolicy

__all__ = [
    "ActivePredicate",
    "CalibrationResult",
    "ExpectedSportsProvider",
    "INPLAY_COVERED_SPORTS",
    "LiveSourceBucket",
    "LiveSourceCalibrator",
    "LiveSourceFeeder",
    "LiveSourceFeederConfig",
    "LiveSourceKey",
    "LiveSourceLifecycleBinder",
    "LiveSourceMatcher",
    "LiveSourceProvider",
    "LiveSourceRegistry",
    "LiveStateMatchService",
    "LiveStateStore",
    "MatchHook",
    "RefreshListener",
    "SnapshotFetcher",
    "SportNormalizer",
    "SportResolver",
    "SportsSubscriptionPolicy",
    "is_market_live_active",
    "provider_priority",
    "source_priority",
]
