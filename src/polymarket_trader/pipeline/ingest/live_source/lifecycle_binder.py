"""LiveSourceLifecycleBinder —— 把 market lifecycle 接到 LiveSourceRegistry。

替代旧 `sports_polling_demand` 的 active_sports_provider 闭包：

- **事件驱动**（C 模式，Step 3a 升级）：通过 `LifecycleRegistry` 监听
  `market_registry.upsert` / `remove_market` 事件，新 market 进入立即
  `subscribe`，prune 立即 `unsubscribe_all`。延迟接近 0。
- **周期 reconcile 兜底**：scheduler 每 2s + discovery 完成 hook 调用
  `reconcile_subscriptions()`，处理事件漏触发 / market 状态变化（如
  game_start_time 到 → active_predicate 翻转）。

# 两条触发路径都必要

- 事件 hook 不能覆盖"market 不动但状态变化"场景（active_predicate 翻转）
- 周期 reconcile 不够及时（最长 2s 延迟）但能自愈

# bind_to_lifecycle_registry vs bind_to_market_registry

新代码用 `bind_to_lifecycle_registry(lifecycle_registry)`，把 prune/added 都注册
到统一的 `LifecycleRegistry`（main.py 把 lifecycle_registry.emit_pruned/added 注
册到 market_registry，所有 fan-out 走单一中枢）。这样：
- main.py 不再有散落的 `market_registry.register_prune_callback`（收敛到
  `lifecycle_registry.register_prune_listener`）
- 测试可独立 mock lifecycle_registry
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from polymarket_trader.domain.market import Market

from .registry import LiveSourceRegistry
from .subscription_policy import SportsSubscriptionPolicy

if TYPE_CHECKING:
    from polymarket_trader.runtime.lifecycle_registry import LifecycleRegistry
    from polymarket_trader.runtime.registry import MarketRegistry

logger = logging.getLogger(__name__)


class LiveSourceLifecycleBinder:
    def __init__(
        self,
        *,
        market_registry: "MarketRegistry",
        live_source_registry: LiveSourceRegistry,
        subscription_policy: SportsSubscriptionPolicy,
    ) -> None:
        self._market_registry = market_registry
        self._registry = live_source_registry
        self._policy = subscription_policy

    def bind_to_lifecycle_registry(self, lifecycle_registry: "LifecycleRegistry") -> None:
        """注册到 LifecycleRegistry 的 prune + added listener（C 模式）。

        统一通过 LifecycleRegistry 接 market lifecycle 事件，main.py 只需一次
        `lifecycle_registry.emit_*` ↔ `market_registry.register_*_callback`
        的桥接，所有 fan-out 在 LifecycleRegistry 内集中管理。
        """

        lifecycle_registry.register_prune_listener("live_source", self._on_market_pruned)
        lifecycle_registry.register_added_listener("live_source", self.on_market_added)

    def on_market_added(self, market: Market) -> None:
        """market 首次进入 universe 时立即订阅 desired sources（事件驱动）。

        与周期 reconcile_subscriptions 等价但 0 延迟——subscribe 是幂等的，事件
        和周期重复触发不会重复订阅同一 source。
        """

        try:
            desired = self._policy.required_sources(market)
        except Exception:  # noqa: BLE001
            logger.exception(
                "subscription_policy failed in on_market_added market=%s",
                market.condition_id,
            )
            return
        for source in desired:
            self._registry.subscribe(source, market.condition_id)

    def reconcile_subscriptions(self) -> dict[str, int]:
        """对比 desired vs current，调整 LiveSourceRegistry 的订阅集。

        返回简单计数 dict 供 supervisor / metric 上报（subscribed / unsubscribed
        本轮各多少条；errors 表示 sport_resolver 异常次数）。
        """

        added = 0
        removed = 0
        errors = 0
        snapshot = self._market_registry.snapshot()
        seen_market_keys: set[str] = set()

        for market in snapshot.markets:
            try:
                desired = frozenset(self._policy.required_sources(market))
            except Exception:  # noqa: BLE001 — 一个 market 异常不阻拦其他
                logger.exception(
                    "subscription_policy failed for market %s",
                    market.condition_id,
                )
                errors += 1
                continue
            market_key = market.condition_id
            seen_market_keys.add(market_key)
            current = self._registry.sources_for_market(market_key)
            for src in current - desired:
                self._registry.unsubscribe(src, market_key)
                removed += 1
            for src in desired - current:
                self._registry.subscribe(src, market_key)
                added += 1

        # 清理那些已经不在 registry.snapshot() 里、但 LiveSourceRegistry 还残留
        # 订阅的 market_key（discovery prune 漏 callback 的兜底）
        orphans = self._collect_orphans(seen_market_keys)
        for orphan_key in orphans:
            self._registry.unsubscribe_all(orphan_key)
            removed += 1

        return {"added": added, "removed": removed, "errors": errors}

    def _on_market_pruned(
        self,
        condition_id: str,
        token_ids: tuple[str, ...],  # noqa: ARG002 — 签名要求，本 binder 不关心 token
    ) -> None:
        self._registry.unsubscribe_all(condition_id)

    def _collect_orphans(self, seen: set[str]) -> tuple[str, ...]:
        # LiveSourceRegistry 没暴露全部 market_key 索引；按 active_sources 反查
        all_subscribers: set[str] = set()
        for source in self._registry.active_sources():
            all_subscribers.update(self._registry.subscribers_for(source))
        return tuple(key for key in all_subscribers if key not in seen)

    def active_sports_provider(self, provider) -> "callable":
        """返回 closure 供 goalserve client 用作 active_sports_provider。

        每次 client 内部评估 demand 时调用，从 LiveSourceRegistry 实时读
        active sport 集合——替代旧 `build_inplay/livescore_active_sports_provider`
        生成的闭包。
        """

        def _provider() -> frozenset[str]:
            return self._registry.active_sports_for(provider)

        return _provider
