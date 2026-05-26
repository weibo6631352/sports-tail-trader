"""LiveSourceRegistry —— 引用计数订阅 + active sport demand 推导。

`SportsSubscriptionPolicy` 决定每个 market 该订哪个 `LiveSourceKey`，registry
维护 (source → market_keys) 集合 + 反向索引 (market_key → sources)，并暴露
`active_sports_for(provider)` 给 feeder 内部的 goalserve client 用作
`active_sports_provider`（demand-driven 自动启停 client per-sport polling task）。

# Registry 不直接启停 feeder

Feeder 自身常驻（每 provider 一个，在 main.py 启动时拉起），通过
`active_sports_provider` 决定**实际 HTTP 请求**——这是 client 内部 per-sport
task 的现有机制，不动。Registry 只通过修改订阅集合间接控制 demand，避免 registry
持有 feeder 引用造成耦合。

# 单 primary source 满足

`SportsSubscriptionPolicy` 保证每个 market 只订 1 个 source（inplay 覆盖的 sport
订 inplay，不覆盖的订 livescore）。但 registry 不强制——多源订阅在 registry 层
仍合法，仅由 policy 层约束。这样未来 policy 演化（如双源对照）不用改 registry。

# 引用计数语义

- `subscribe(source, market)`：market 加入该 source 的订阅集合
- `unsubscribe(source, market)`：market 退订；source 集合空了 → fan-out 通知（让 feeder 知道该 sport 不再 active）
- `unsubscribe_all(market)`：market prune 时一键退订所有 source
- `active_sports_for(provider)`：返回该 provider 当前还有订阅的 sport 集合
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from threading import Lock

from .source import LiveSourceKey, LiveSourceProvider

logger = logging.getLogger(__name__)

ChangeListener = Callable[[LiveSourceKey], None]


class LiveSourceRegistry:
    def __init__(self) -> None:
        self._subs: dict[LiveSourceKey, set[str]] = defaultdict(set)
        self._market_to_sources: dict[str, set[LiveSourceKey]] = defaultdict(set)
        self._listeners: list[ChangeListener] = []
        self._lock = Lock()

    def subscribe(self, source: LiveSourceKey, market_key: str) -> None:
        notify = False
        with self._lock:
            subs = self._subs[source]
            was_empty = not subs
            subs.add(market_key)
            self._market_to_sources[market_key].add(source)
            notify = was_empty
        if notify:
            self._fan_out(source)

    def unsubscribe(self, source: LiveSourceKey, market_key: str) -> None:
        became_empty = False
        with self._lock:
            subs = self._subs.get(source)
            if not subs:
                return
            subs.discard(market_key)
            sources = self._market_to_sources.get(market_key)
            if sources is not None:
                sources.discard(source)
                if not sources:
                    self._market_to_sources.pop(market_key, None)
            if not subs:
                self._subs.pop(source, None)
                became_empty = True
        if became_empty:
            self._fan_out(source)

    def unsubscribe_all(self, market_key: str) -> None:
        emptied: list[LiveSourceKey] = []
        with self._lock:
            sources = self._market_to_sources.pop(market_key, None)
            if not sources:
                return
            for source in tuple(sources):
                subs = self._subs.get(source)
                if subs is None:
                    continue
                subs.discard(market_key)
                if not subs:
                    self._subs.pop(source, None)
                    emptied.append(source)
        for source in emptied:
            self._fan_out(source)

    def subscribers_for(self, source: LiveSourceKey) -> frozenset[str]:
        with self._lock:
            return frozenset(self._subs.get(source, ()))

    def active_sources(self) -> frozenset[LiveSourceKey]:
        with self._lock:
            return frozenset(self._subs.keys())

    def active_sports_for(self, provider: LiveSourceProvider) -> frozenset[str]:
        with self._lock:
            return frozenset(
                source.sport
                for source in self._subs
                if source.provider == provider
            )

    def sources_for_market(self, market_key: str) -> frozenset[LiveSourceKey]:
        with self._lock:
            return frozenset(self._market_to_sources.get(market_key, ()))

    def summary(self) -> dict[str, dict[str, int]]:
        """admin / metric 用：每个 source 当前 subscriber 数。"""

        with self._lock:
            return {
                source.as_label(): {"subscriber_count": len(subs)}
                for source, subs in self._subs.items()
            }

    def register_change_listener(self, listener: ChangeListener) -> None:
        """source 进入或退出 active 集时回调（首个 subscriber / 末个退订）。"""

        with self._lock:
            self._listeners.append(listener)

    def _fan_out(self, source: LiveSourceKey) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(source)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "LiveSourceRegistry change listener failed for source=%s",
                    source.as_label(),
                )
