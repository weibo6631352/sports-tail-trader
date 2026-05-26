"""派生指标 publisher——同步算 + 同步写 store。

P0 量化决策需要"snapshot 与 derived 的新鲜度对齐":
策略 hook 接到 ORDERBOOK_SNAPSHOT_UPDATED 事件时, derived store 必须已含
当前 snapshot 对应的派生指标, 而非异步任务还在算的旧版。

实测 ``compute_derived`` 单次 ~245μs (含 60 档双边 + 50 sample history),
即使 1000 push/s 也只占 ~25% 单核 CPU——完全可在 ws 推送同 task 内同步算。

P0 影响审计:
- ws push 主路径同 task 内多 ~0.2-1ms (含 history.samples 拷贝).
- emit_event 之前已写好 store, 下游 P0 决策读 store 拿到的就是新鲜值.
- 同 task 内顺序执行, 没有 race / 写脏 / version stamp 等异步并发问题.
- 异常被 catch + 记 stats, 不影响后续推送 (单次 compute 失败不阻塞 P0).

组合优化 (#75):
publisher 拼装 OFI 风向信号嵌入 DerivedMetrics, 让 operator /markets/orderbook-depth
一站式拿到所有可观测信号 (深度 + 流动性 + 滑点 + 风向). delta_store 仍独立服务
P0 策略层 (按需 direction_signal 查询), 算法不重复.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.orderbook_derived import (
    DirectionSnapshot,
    compute_derived,
)
from polymarket_trader.observability.cpu_track import cpu_track, step_track

if TYPE_CHECKING:
    from polymarket_trader.runtime.orderbook_delta import OrderbookDeltaStore
    from polymarket_trader.runtime.orderbook_derived_store import OrderbookDerivedStore
    from polymarket_trader.runtime.orderbook_history_buffer import OrderbookHistoryBuffer

logger = logging.getLogger(__name__)

# direction_signal 默认窗口 (与 operator /markets/orderbook-direction 端点一致, 10s).
# 嵌入 DerivedMetrics 用统一窗口, 不参数化, 保持 derived payload 字段稳定.
_DIRECTION_WINDOW_SECONDS = 10.0


class OrderbookDerivedPublisher:
    """订阅 ws push, 同步算 ``DerivedMetrics`` 写入 store。

    组合: 内部调 ``delta_store.direction_signal()`` 拼装 OFI 风向投影一并嵌入,
    避免 operator 多次 round-trip 同时不重复算法。
    """

    def __init__(
        self,
        *,
        store: "OrderbookDerivedStore",
        history_buffer: "OrderbookHistoryBuffer",
        delta_store: "OrderbookDeltaStore | None" = None,
    ) -> None:
        self._store = store
        self._history_buffer = history_buffer
        # delta_store 可选: None 时 derived.direction 永远 None (operator 仍可独立调
        # /markets/orderbook-direction 看 raw signal). 生产环境总会注入.
        self._delta_store = delta_store
        # observability
        self._refreshed_total: int = 0
        self._error_total: int = 0

    @cpu_track("derived_publisher")
    def refresh(self, snapshot: OrderbookSnapshot) -> None:
        """ws push 主路径同步入口: 算出 derived 写 store, 调用方接着发事件。

        P0 量化决策不变量: refresh 返回后 ``store.get(snapshot.token_id)`` 必含
        基于本次 snapshot 的最新派生指标。
        """
        try:
            with step_track("derived_publisher", "fetch_history"):
                samples = self._history_buffer.samples(snapshot.token_id)
            with step_track("derived_publisher", "fetch_direction"):
                direction = self._build_direction_snapshot(snapshot.token_id)
            with step_track("derived_publisher", "compute"):
                derived = compute_derived(snapshot, samples, direction=direction)
            with step_track("derived_publisher", "store_set"):
                self._store.set(derived)
            self._refreshed_total += 1
        except Exception as exc:
            # P0 推送链路上的异常必须吞掉, 不能让一个 token 的 derived 计算失败
            # 阻塞所有 token 推送 / event emit. 记 stats + warn 供后续追查.
            self._error_total += 1
            logger.warning(
                "derived_refresh_failed token_id=%s err=%s",
                snapshot.token_id, exc,
            )

    def _build_direction_snapshot(self, token_id: str) -> DirectionSnapshot | None:
        """从 delta_store 拉 10s 窗口 direction_signal, 投影到 DerivedMetrics 嵌入字段。

        sample 不足 / token 首次推送 → None (DerivedMetrics.direction 也是 None,
        operator 看到 null 表示信号还不可用)。
        """
        if self._delta_store is None:
            return None
        signal = self._delta_store.direction_signal(
            token_id, window_seconds=_DIRECTION_WINDOW_SECONDS
        )
        if signal is None:
            return None
        return DirectionSnapshot(
            window_seconds=signal.window_seconds,
            sample_count=signal.sample_count,
            direction_score=signal.direction_score,
            price_momentum=signal.price_momentum,
            flow_imbalance=signal.flow_imbalance,
            direction_label=signal.direction_label,
            confidence=signal.confidence,
        )

    def stats(self) -> dict[str, int]:
        """observability: /runtime/memory-components 或 system-perf 用。"""
        return {
            "refreshed_total": self._refreshed_total,
            "error_total": self._error_total,
        }


__all__ = ["OrderbookDerivedPublisher"]
