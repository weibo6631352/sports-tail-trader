"""内存盘口快照 ring buffer——为多窗口波动指标提供 lookup。

每 token_id 维护一个滚动 deque[(ts_monotonic, snapshot)]，capacity 由 max_age_s
控制（默认 15s，足够 2/3/5/10s delta 计算）。append 是 O(1)，lookup 按时间窗
线性扫（典型 < 50 个样本，~微秒）。

内存上限设计:
- 每 token 最多 ``max_samples_per_token`` 条(deque maxlen).
- 全局最多 ``max_tokens`` 个 token,OrderedDict LRU evict 最久未访问的整桶.
- 单条 snapshot ~1KB(含 levels);默认 200 samples × 2000 tokens = ~400MB 上限,
  实际订阅 < 500 markets 时 ~100MB.

P0 零依赖:纯 dict + deque,无锁,靠 GIL 原子性。market_ws _dispatch_message
在每次 snapshot 更新后调一次 ``record()``,不阻塞主链路。
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from polymarket_trader.domain.orderbook import OrderbookSnapshot


@dataclass(slots=True)
class _Sample:
    ts_mono: float
    snapshot: OrderbookSnapshot


class OrderbookHistoryBuffer:
    """每 token_id 滚动盘口快照 ring buffer,全局 LRU 限制 token 总数。"""

    def __init__(
        self,
        *,
        max_age_s: float = 15.0,
        max_tokens: int = 2000,
    ) -> None:
        # 纯时间窗 popleft:超 max_age_s 就丢,不用 deque maxlen.
        # max_tokens=2000:正常订阅市场数 < 500,LRU evict 防极端 token 数爆.
        self._max_age_s = max_age_s
        self._max_tokens = max_tokens
        # OrderedDict 实现 token 维度 LRU
        self._buffers: OrderedDict[str, deque[_Sample]] = OrderedDict()

    def record(self, snapshot: OrderbookSnapshot) -> None:
        token_id = snapshot.token_id
        buf = self._buffers.get(token_id)
        now_mono = time.monotonic()
        if buf is None:
            buf = deque()
            self._buffers[token_id] = buf
        else:
            self._buffers.move_to_end(token_id)
        buf.append(_Sample(ts_mono=now_mono, snapshot=snapshot))
        # 时间窗剪枝:超 max_age_s 就 popleft
        cutoff = now_mono - self._max_age_s
        while buf and buf[0].ts_mono < cutoff:
            buf.popleft()
        # LRU evict:token 数超上限,踢最久未访问的整桶
        while len(self._buffers) > self._max_tokens:
            self._buffers.popitem(last=False)

    def snapshot_at(self, token_id: str, *, ago_s: float) -> OrderbookSnapshot | None:
        """返回 ``ago_s`` 秒前最接近的 snapshot(找不到返回 None)。

        线性扫:数据量小(< 50 样本)O(n)即 < 微秒.
        """
        buf = self._buffers.get(token_id)
        if not buf:
            return None
        target = time.monotonic() - ago_s
        # 从最旧开始找第一个 ts >= target,前一个就是最接近 target 的旧样本
        best: OrderbookSnapshot | None = None
        best_diff = float("inf")
        for sample in buf:
            diff = abs(sample.ts_mono - target)
            if diff < best_diff:
                best_diff = diff
                best = sample.snapshot
            else:
                break  # buf 按 ts 升序,diff 开始增长说明过了 target
        return best

    def sample_count(self, token_id: str) -> int:
        buf = self._buffers.get(token_id)
        return 0 if buf is None else len(buf)

    def samples(self, token_id: str) -> list[tuple[float, OrderbookSnapshot]]:
        """返回 ``token_id`` 当前窗口内的样本拷贝 ``[(ts_mono, snapshot), ...]``,
        按时间升序。

        调用方拿到独立 list 后可跨线程使用 (传给 ``asyncio.to_thread``);
        deque 本身在主 loop thread 内继续追加, 不影响已拷贝的 list。
        snapshot 是 frozen dataclass, 引用拷贝安全。
        """
        buf = self._buffers.get(token_id)
        if not buf:
            return []
        return [(s.ts_mono, s.snapshot) for s in buf]

    def tracked_token_count(self) -> int:
        return len(self._buffers)

    def memory_footprint_estimate(self) -> dict[str, int]:
        """容量+实际 token 数+样本总数,admin 观测内存使用."""
        total_samples = sum(len(b) for b in self._buffers.values())
        return {
            "tracked_tokens": len(self._buffers),
            "max_tokens": self._max_tokens,
            "total_samples": total_samples,
            "max_age_s": int(self._max_age_s),
        }


__all__ = ["OrderbookHistoryBuffer"]
