from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import Lock
from typing import TYPE_CHECKING, Callable

from polymarket_trader.domain.market import Market, TradingStatus

if TYPE_CHECKING:
    from polymarket_trader.runtime.market_companion_state import MarketCompanionState

logger = logging.getLogger(__name__)

# 写锁 acquire 等超过这个阈值就 WARN——P0 主链路在 event loop 上跑，从
# async caller 调写方法（market_ws_worker 等）时如果锁被持有，整个 loop
# 阻塞。50ms 的超时其实是兜底，正常争用应该 < 1ms；阈值留 5ms 给瞬时抖动。
_LOCK_ACQUIRE_WARN_S = 0.005


# === 为什么不用 asyncio.Lock？===
# 1. registry 写路径同时被 event loop 协程（reconcile / discovery worker）和
#    thread pool worker（market_ws 反序列化线程）调用——asyncio.Lock 只在
#    event loop 内有效，跨线程会失效。
# 2. 写本身是几个 dict 复制 + 一次属性赋值（~微秒级），GIL 已经保证单步原子；
#    threading.Lock 主要防的是「跨线程同时写不同 condition_id 的 dict copy
#    互相覆盖」，这种 race 即使在 asyncio.Lock 下也得用 threading 锁。
# 3. acquire 超 _LOCK_ACQUIRE_WARN_S 会 log warning，真有 event loop 阻塞
#    问题运维侧能立刻看到。


@dataclass(frozen=True, slots=True)
class MarketRegistrySnapshot:
    markets: tuple[Market, ...]

    def get_by_condition_id(self, condition_id: str) -> Market | None:
        for market in self.markets:
            if market.condition_id == condition_id:
                return market
        return None

    def get_by_token_id(self, token_id: str) -> Market | None:
        for market in self.markets:
            if token_id in market.token_ids:
                return market
        return None

    def get_by_slug(self, slug: str) -> Market | None:
        for market in self.markets:
            if market.market_slug == slug or market.event_slug == slug:
                return market
        return None


class MarketRegistry:
    """Sharded market registry with lock-free reads and short write commits."""

    def __init__(self) -> None:
        self._markets_by_condition_id: dict[str, Market] = {}
        self._condition_id_by_token_id: dict[str, str] = {}
        self._condition_id_by_slug: dict[str, str] = {}
        self._condition_id_by_event_slug: dict[str, str] = {}
        # companion state(audit dedupe + 节流计时器),lifecycle 严格随 market:
        # upsert 时若 cid 首次出现自动创建,remove_market 时同步 pop → 不留泄漏.
        from polymarket_trader.runtime.market_companion_state import MarketCompanionState
        self._companion_cls = MarketCompanionState
        self._companions: dict[str, MarketCompanionState] = {}
        # 首次 tracked 的 monotonic 时间, 用于 reconcile 判断"market tracked N 秒后
        # 仍无 live source 匹配 → prune". remove_market 时同步 pop.
        self._first_tracked_at_mono: dict[str, float] = {}
        # market prune callback(condition_id, token_ids) - workers 自己注册清自己的
        # cid/token 索引 dict;避免散落各处忘记 prune 联动.
        self._prune_callbacks: list[Callable[[str, tuple[str, ...]], None]] = []
        # market 首次进入 universe 时调用的 added callback(market). 用于"新 market
        # 进入立即订阅直播源 / odds 等"事件驱动联动 (LifecycleRegistry 的 added 通路).
        self._added_callbacks: list[Callable[[Market], None]] = []
        self._snapshot = MarketRegistrySnapshot(tuple())
        self._shard_locks: dict[str, Lock] = {}
        self._locks_lock = Lock()
        self._commit_lock = Lock()

    def register_prune_callback(self, cb: "Callable[[str, tuple[str, ...]], None]") -> None:
        """workers 在 init 时注册 prune 回调.

        signature: cb(condition_id: str, token_ids: tuple[str, ...]).
        market 被 remove_market 时同步调用所有 cb,worker 自己清自己的 dict.

        生产路径建议改为通过 LifecycleRegistry 统一接入（main.py 只把
        lifecycle_registry.emit_pruned 注册到本 callback，9+ 处散落注册收敛
        到 LifecycleRegistry.register_prune_listener）。
        """
        self._prune_callbacks.append(cb)

    def register_added_callback(self, cb: "Callable[[Market], None]") -> None:
        """market 首次进入 universe 时触发的 callback.

        signature: cb(market: Market).
        only triggered on first appearance of condition_id (subsequent upsert
        of same cid 不触发——避免 reconcile 周期性 upsert 重复 fan-out).

        生产路径建议改为通过 LifecycleRegistry 统一接入：main.py 只把
        lifecycle_registry.emit_added 注册到本 callback。
        """
        self._added_callbacks.append(cb)

    def first_tracked_at_mono(self, condition_id: str) -> float | None:
        """首次 tracked 的 monotonic 时间戳, 用于算 tracked_for_seconds."""
        return self._first_tracked_at_mono.get(condition_id)

    def companion(self, condition_id: str) -> "MarketCompanionState | None":
        """读 companion(market 已 prune 返回 None,调用方应跳过操作).

        故意不 lazy-create:返回 None 说明 cid 不属于活跃 market,
        worker 也不应该再写 dedupe state.
        """
        return self._companions.get(condition_id)

    def upsert(self, market: Market, *, timeout: float = 0.05) -> None:
        self._with_condition_lock(market.condition_id, timeout, lambda: self._upsert_locked(market))

    def reconcile(self, market: Market, *, timeout: float = 0.05) -> Market | None:
        return self._with_condition_lock(
            market.condition_id,
            timeout,
            lambda: self._upsert_locked(market),
        )

    def get_by_condition_id(self, condition_id: str, *, timeout: float = 0.05) -> Market | None:
        # 读取走 copy-on-write 快照引用，不和 P0 写路径竞争全局索引锁。
        return self._markets_by_condition_id.get(condition_id)

    def get_by_token_id(self, token_id: str, *, timeout: float = 0.05) -> Market | None:
        condition_id = self._condition_id_by_token_id.get(token_id)
        if condition_id is None:
            return None
        return self._markets_by_condition_id.get(condition_id)

    def get_by_slug(self, slug: str, *, timeout: float = 0.05) -> Market | None:
        condition_id = self._condition_id_by_slug.get(slug)
        if condition_id is None:
            condition_id = self._condition_id_by_event_slug.get(slug)
        if condition_id is None:
            return None
        return self._markets_by_condition_id.get(condition_id)

    def snapshot(self) -> MarketRegistrySnapshot:
        return self._snapshot

    def remove_market(self, condition_id: str, *, timeout: float = 0.05) -> Market | None:
        return self._with_condition_lock(
            condition_id,
            timeout,
            lambda: self._remove_market_locked(condition_id),
        )

    def set_trading_status(
        self,
        condition_id: str,
        trading_status: TradingStatus,
        *,
        reject_reason: str | None = None,
        timeout: float = 0.05,
    ) -> Market | None:
        return self._with_condition_lock(
            condition_id,
            timeout,
            lambda: self._update_market_locked(
                condition_id,
                lambda market: market.with_trading_status(
                    trading_status,
                    reject_reason=reject_reason,
                ),
            ),
        )

    def change_tick_size(
        self,
        condition_id: str,
        tick_size: Decimal,
        *,
        timeout: float = 0.05,
    ) -> Market | None:
        return self._with_condition_lock(
            condition_id,
            timeout,
            lambda: self._update_market_locked(
                condition_id,
                lambda market: market.with_tick_size(tick_size),
            ),
        )

    def change_min_order_size(
        self,
        condition_id: str,
        min_order_size: Decimal,
        *,
        timeout: float = 0.05,
    ) -> Market | None:
        return self._with_condition_lock(
            condition_id,
            timeout,
            lambda: self._update_market_locked(
                condition_id,
                lambda market: market.with_min_order_size(min_order_size),
            ),
        )

    def update_fee_schedule(
        self,
        condition_id: str,
        *,
        fees_enabled: bool | None = None,
        maker_base_fee_bps: int | None = None,
        taker_base_fee_bps: int | None = None,
        timeout: float = 0.05,
    ) -> Market | None:
        return self._with_condition_lock(
            condition_id,
            timeout,
            lambda: self._update_market_locked(
                condition_id,
                lambda market: market.with_fee_schedule(
                    fees_enabled=fees_enabled,
                    maker_base_fee_bps=maker_base_fee_bps,
                    taker_base_fee_bps=taker_base_fee_bps,
                ),
            ),
        )

    def update_fee_rate(
        self,
        condition_id: str,
        fee_rate_bps: int | None,
        *,
        fee_rate_updated_at: datetime | None = None,
        timeout: float = 0.05,
    ) -> Market | None:
        return self._with_condition_lock(
            condition_id,
            timeout,
            lambda: self._update_market_locked(
                condition_id,
                lambda market: market.with_fee_rate(
                    fee_rate_bps,
                    fee_rate_updated_at=fee_rate_updated_at,
                ),
            ),
        )

    def mark_resolved(self, condition_id: str, *, timeout: float = 0.05) -> Market | None:
        # 市场一旦 resolved，交易状态就以结算结果为真相来源，后续只允许快照读取和归档。
        return self.set_trading_status(condition_id, TradingStatus.RESOLVED, timeout=timeout)

    def mark_closed(self, condition_id: str, *, timeout: float = 0.05) -> Market | None:
        return self.set_trading_status(condition_id, TradingStatus.CLOSED, timeout=timeout)

    def disable_orderbook(
        self,
        condition_id: str,
        *,
        timeout: float = 0.05,
        reason: str = "orderbook_disabled",
    ) -> Market | None:
        # orderbook disabled 不是业务拒绝，而是交易路径的临时停用信号，所以落到 paused。
        return self.set_trading_status(
            condition_id,
            TradingStatus.PAUSED,
            reject_reason=reason,
            timeout=timeout,
        )

    def pause_market(
        self,
        condition_id: str,
        *,
        timeout: float = 0.05,
        reason: str = "manual_pause",
    ) -> Market | None:
        return self.set_trading_status(
            condition_id,
            TradingStatus.PAUSED,
            reject_reason=reason,
            timeout=timeout,
        )

    def resume_market(self, condition_id: str, *, timeout: float = 0.05) -> Market | None:
        market = self.get_by_condition_id(condition_id, timeout=timeout)
        if market is None or market.trading_status not in {
            TradingStatus.PAUSED,
            TradingStatus.CANDIDATE,
        }:
            return market
        # 人工恢复只把市场拉回 eligible，不覆盖已终态结算信息。
        return self.set_trading_status(
            condition_id,
            TradingStatus.ELIGIBLE,
            reject_reason=None,
            timeout=timeout,
        )

    def reject_market(
        self,
        condition_id: str,
        *,
        reject_reason: str,
        timeout: float = 0.05,
    ) -> Market | None:
        return self.set_trading_status(
            condition_id,
            TradingStatus.REJECTED,
            reject_reason=reject_reason,
            timeout=timeout,
        )

    def _upsert_locked(self, market: Market) -> Market | None:
        is_new = False
        with self._commit_lock:
            markets_by_condition_id = dict(self._markets_by_condition_id)
            condition_id_by_token_id = dict(self._condition_id_by_token_id)
            condition_id_by_slug = dict(self._condition_id_by_slug)
            condition_id_by_event_slug = dict(self._condition_id_by_event_slug)

            previous = markets_by_condition_id.get(market.condition_id)
            if previous is not None:
                self._detach_indexes(
                    previous,
                    condition_id_by_token_id,
                    condition_id_by_slug,
                    condition_id_by_event_slug,
                )

            markets_by_condition_id[market.condition_id] = market
            for token_id in market.token_ids:
                condition_id_by_token_id[token_id] = market.condition_id
            condition_id_by_slug[market.market_slug] = market.condition_id
            if market.event_slug:
                condition_id_by_event_slug.setdefault(market.event_slug, market.condition_id)

            # 首次出现 cid 时创建 companion(已存在则保留旧 state,避免 reconnect/upsert
            # 触发 audit 重发)。is_new 是 added callback 的触发条件——只在新 cid
            # 首次入 universe 时 fan-out，后续 upsert 同 cid 不重复触发。
            if market.condition_id not in self._companions:
                self._companions[market.condition_id] = self._companion_cls()
                self._first_tracked_at_mono[market.condition_id] = time.monotonic()
                is_new = True

            self._publish_state(
                markets_by_condition_id,
                condition_id_by_token_id,
                condition_id_by_slug,
                condition_id_by_event_slug,
            )
        # commit_lock 已释放,在 lock 外触发 added callbacks（避免 cb 阻塞索引锁;
        # 捕获异常防一个 cb 挂掉影响其他）
        if is_new:
            for cb in self._added_callbacks:
                try:
                    cb(market)
                except Exception as exc:
                    logger.warning("added_callback failed: %s", exc)
        return market

    def _update_market_locked(
        self,
        condition_id: str,
        updater: Callable[[Market], Market],
    ) -> Market | None:
        with self._commit_lock:
            current = self._markets_by_condition_id.get(condition_id)
            if current is None:
                return None

            updated = updater(current)
            markets_by_condition_id = dict(self._markets_by_condition_id)
            condition_id_by_token_id = dict(self._condition_id_by_token_id)
            condition_id_by_slug = dict(self._condition_id_by_slug)
            condition_id_by_event_slug = dict(self._condition_id_by_event_slug)

            self._detach_indexes(
                current,
                condition_id_by_token_id,
                condition_id_by_slug,
                condition_id_by_event_slug,
            )
            markets_by_condition_id[condition_id] = updated
            for token_id in updated.token_ids:
                condition_id_by_token_id[token_id] = updated.condition_id
            condition_id_by_slug[updated.market_slug] = updated.condition_id
            if updated.event_slug:
                condition_id_by_event_slug.setdefault(updated.event_slug, updated.condition_id)

            self._publish_state(
                markets_by_condition_id,
                condition_id_by_token_id,
                condition_id_by_slug,
                condition_id_by_event_slug,
            )
            return updated

    def _remove_market_locked(self, condition_id: str) -> Market | None:
        with self._commit_lock:
            current = self._markets_by_condition_id.get(condition_id)
            if current is None:
                return None

            markets_by_condition_id = dict(self._markets_by_condition_id)
            condition_id_by_token_id = dict(self._condition_id_by_token_id)
            condition_id_by_slug = dict(self._condition_id_by_slug)
            condition_id_by_event_slug = dict(self._condition_id_by_event_slug)

            self._detach_indexes(
                current,
                condition_id_by_token_id,
                condition_id_by_slug,
                condition_id_by_event_slug,
            )
            markets_by_condition_id.pop(condition_id, None)
            # 联动清 companion:lifecycle 绑定保证 prune 后 dedupe state 自然消失,
            # 无需各 worker 各自 sync.
            self._companions.pop(condition_id, None)
            self._first_tracked_at_mono.pop(condition_id, None)
            self._publish_state(
                markets_by_condition_id,
                condition_id_by_token_id,
                condition_id_by_slug,
                condition_id_by_event_slug,
            )
            # 触发 prune callbacks:workers 清自己的 cid/token 索引 dict.
            # 在 commit lock 外执行 cb,避免 cb 阻塞索引锁;捕获异常防一个 cb 挂掉影响其他.
            tokens = current.token_ids or ()
        # commit_lock 已释放,在这里触发 callbacks
        for cb in self._prune_callbacks:
            try:
                cb(condition_id, tokens)
            except Exception as exc:
                logger.warning("prune_callback failed: %s", exc)
        return current

    def _detach_indexes(
        self,
        market: Market,
        condition_id_by_token_id: dict[str, str],
        condition_id_by_slug: dict[str, str],
        condition_id_by_event_slug: dict[str, str],
    ) -> None:
        for token_id in market.token_ids:
            if condition_id_by_token_id.get(token_id) == market.condition_id:
                condition_id_by_token_id.pop(token_id, None)
        if condition_id_by_slug.get(market.market_slug) == market.condition_id:
            condition_id_by_slug.pop(market.market_slug, None)
        if market.event_slug and condition_id_by_event_slug.get(market.event_slug) == market.condition_id:
            condition_id_by_event_slug.pop(market.event_slug, None)

    def _publish_state(
        self,
        markets_by_condition_id: dict[str, Market],
        condition_id_by_token_id: dict[str, str],
        condition_id_by_slug: dict[str, str],
        condition_id_by_event_slug: dict[str, str],
    ) -> None:
        self._markets_by_condition_id = markets_by_condition_id
        self._condition_id_by_token_id = condition_id_by_token_id
        self._condition_id_by_slug = condition_id_by_slug
        self._condition_id_by_event_slug = condition_id_by_event_slug
        self._snapshot = MarketRegistrySnapshot(tuple(markets_by_condition_id.values()))

    def _with_condition_lock(
        self,
        condition_id: str,
        timeout: float,
        action: Callable[[], Market | None],
    ) -> Market | None:
        lock = self._condition_lock(condition_id)
        started = time.monotonic()
        acquired = lock.acquire(timeout=timeout)
        elapsed = time.monotonic() - started
        if not acquired:
            # 拿不到锁就跳过这次写——交给下一轮 reconcile 兜底。CLAUDE.md §7
            # 「非关键锁等待超时后跳过并告警，不能无限等待」。
            logger.warning(
                "registry write skipped: condition_id=%s lock_timeout_s=%.3f (held > %.3fs)",
                condition_id,
                timeout,
                timeout,
            )
            return None
        if elapsed > _LOCK_ACQUIRE_WARN_S:
            # 真正发生争用（event loop 在 acquire 期间 stuck）才告警；正常情况
            # threading.Lock 在 GIL 下 acquire 是纳秒级。
            logger.warning(
                "registry lock contention: condition_id=%s acquire_s=%.4f (warn_threshold=%.4fs)",
                condition_id,
                elapsed,
                _LOCK_ACQUIRE_WARN_S,
            )
        try:
            return action()
        finally:
            lock.release()

    def _condition_lock(self, condition_id: str) -> Lock:
        with self._locks_lock:
            lock = self._shard_locks.get(condition_id)
            if lock is None:
                lock = Lock()
                self._shard_locks[condition_id] = lock
            return lock
