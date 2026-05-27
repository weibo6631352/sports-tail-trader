"""SignalSnapshotStore —— LiveSignalSnapshot 的进程内 ring buffer。

CPO Round 2 R6 范围：每 token 最近 N=300 个 snapshot，约 5 分钟窗口（实测决策
触发频率 1 snapshot/s/token）。**无 DB 写入**——纯内存、纯 observability，
重启即重置。

并发模型：asyncio 单线程，无锁。多 token 共用一份 store，按 token_id 分桶。
size 上限按桶（per token）控制，不在 store 全局——避免某个高频 token 把其他
token 的历史挤掉。

读路径（``/analytics/edge-signals``）按 ``edge_pp desc`` 排序拿 top-K，零拷贝
返回 dataclass 实例。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from polymarket_trader.domain.signal_snapshot import LiveSignalSnapshot

# 每 token 保留的最大 snapshot 数（5 分钟 × 60 s × ~1 snapshot/s ≈ 300）。
# 超出 → deque 自动从左端淘汰最老一条。
_PER_TOKEN_RING_CAPACITY = 300


class SignalSnapshotStore:
    """Per-token ring buffer of LiveSignalSnapshot。"""

    __slots__ = ("_per_token",)

    def __init__(self) -> None:
        self._per_token: dict[str, deque[LiveSignalSnapshot]] = {}

    def record(self, snapshot: LiveSignalSnapshot) -> None:
        """写入一条 snapshot，按 token_id 分桶，超出 capacity 自动淘汰最老。"""

        token_id = snapshot.token_id
        bucket = self._per_token.get(token_id)
        if bucket is None:
            bucket = deque(maxlen=_PER_TOKEN_RING_CAPACITY)
            self._per_token[token_id] = bucket
        bucket.append(snapshot)

    def latest_for_token(self, token_id: str) -> LiveSignalSnapshot | None:
        """取某 token 的最新一条 snapshot（None = 该 token 无记录）。"""

        bucket = self._per_token.get(token_id)
        if not bucket:
            return None
        return bucket[-1]

    def history_for_token(self, token_id: str) -> tuple[LiveSignalSnapshot, ...]:
        """取某 token 的全部历史 snapshot（按写入顺序）。"""

        bucket = self._per_token.get(token_id)
        return tuple(bucket) if bucket else ()

    def all_latest(self) -> Iterable[LiveSignalSnapshot]:
        """跨所有 token 取每个的最新一条——top-K edge ranking 数据源。"""

        for bucket in self._per_token.values():
            if bucket:
                yield bucket[-1]

    def top_by_edge(self, k: int = 10) -> tuple[LiveSignalSnapshot, ...]:
        """按 ``edge_pp desc`` 排序取 top-K 差价候选。

        edge_pp=None 的 snapshot（goalserve 或 ask 缺）自动剔除——它们没有
        可比对的 edge 信号，不应出现在 ranking。
        """

        ranked = [
            snap for snap in self.all_latest()
            if snap.edge_pp is not None and snap.edge_pp > 0
        ]
        ranked.sort(key=lambda s: s.edge_pp or 0, reverse=True)
        return tuple(ranked[:max(0, k)])

    def token_count(self) -> int:
        """当前 store 内有多少 token 有过 snapshot 记录（observability 指标）。"""

        return len(self._per_token)


__all__ = ["SignalSnapshotStore"]
