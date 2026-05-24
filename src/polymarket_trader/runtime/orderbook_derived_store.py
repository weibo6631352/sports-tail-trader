"""派生指标内存 store——由 ``derived publisher`` 写入, admin endpoint 只读。

每 token_id 缓存一份 ``DerivedMetrics``: 由 market_ws 推送时通过 publisher 重算
并替换 (cover-write, 无 delta merge)。lifecycle 跟随 market_registry: 注册
``evict_market`` 到 ``register_prune_callback``, market 被 remove 时同步清。

容量上限:
- ``max_tokens=2000``: 与 ``OrderbookHistoryBuffer`` 同口径, 正常 subscribed
  market < 500, 极端 churn 也不会过 2000.
- 超 cap → OrderedDict LRU 踢最久未访问 token (set 时 move_to_end).

P0 零依赖: 纯 dict + 一把 Lock (短临界区), admin 读不阻塞 ws 推送写。
"""
from __future__ import annotations

from collections import OrderedDict
from threading import Lock

from polymarket_trader.domain.orderbook_derived import DerivedMetrics


class OrderbookDerivedStore:
    """token_id → DerivedMetrics 单层内存 cache, LRU + prune callback 联动。"""

    def __init__(self, *, max_tokens: int = 2000) -> None:
        self._max_tokens = max_tokens
        self._lock = Lock()
        self._cache: OrderedDict[str, DerivedMetrics] = OrderedDict()

    def set(self, derived: DerivedMetrics) -> None:
        """覆盖写入: 新值替换旧值, 同时更新 LRU 顺序。"""
        token_id = derived.token_id
        with self._lock:
            self._cache[token_id] = derived
            self._cache.move_to_end(token_id)
            while len(self._cache) > self._max_tokens:
                self._cache.popitem(last=False)

    def get(self, token_id: str) -> DerivedMetrics | None:
        """读取不更新 LRU 顺序: admin 读不应影响 publisher 的 evict 优先级。"""
        return self._cache.get(token_id)

    def evict_market(self, condition_id: str, token_ids: tuple[str, ...]) -> None:
        """registry prune callback: market 被 remove 时同步清该 cid 的 token 缓存。

        token_ids 由 registry 在 remove_market 时传入, 直接 pop 即可,
        不依赖反向索引。``condition_id`` 仅用于诊断, 实际清理按 token_ids 走。
        """
        with self._lock:
            for tid in token_ids:
                self._cache.pop(tid, None)

    def tracked_token_count(self) -> int:
        return len(self._cache)

    def memory_footprint_estimate(self) -> dict[str, int]:
        """容量+实际 token 数, admin /runtime/memory-components 用。"""
        return {
            "tracked_tokens": len(self._cache),
            "max_tokens": self._max_tokens,
        }


__all__ = ["OrderbookDerivedStore"]
