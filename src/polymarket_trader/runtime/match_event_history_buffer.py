"""比赛事件流 buffer——离散事件流时序信号源。

与 ``OrderbookHistoryBuffer`` 的连续采样不同：比赛事件（进球 / 红黄牌 /
换人 / VAR 取消）是离散事件，按"赛事生命周期"保留整场——市场 settled
后由清理回调一次性 drop。

key 用 ``condition_id``（一个 Polymarket 市场绑定一场比赛），而非 LiveEvent
source_event_id：策略侧拿到 DecisionContext 时直接用 market.condition_id 查。

# 数据结构

``OrderedDict[condition_id → OrderedDict[fingerprint → SoccerMatchEvent]]``：

- 内层 ``OrderedDict[fp, event]`` 一物三用——天然 dedup、保留插入序、O(1)
  popitem(last=False) 丢最老。**避免** "deque + 独立 fingerprint set" 双结构
  的同步漂移与 set 无界增长。
- fingerprint = ``(event_type, player_id, team, minute, score_after)``。
  ``minute`` 保留原始字符串（含 ``"90+3"`` 补时表达），不会让点球 + 阵地战
  同一分钟双进球碰撞。
- 超 ``max_events_per_market`` → ``popitem(last=False)`` 同步移除最老 fingerprint
  与对应事件，无泄漏、无静默丢（被丢的是最旧的，调用方拿到的始终是末尾窗口）。
- ``events()`` 读操作不调 ``move_to_end``——读不扰外层 LRU，evict 仍是"最久
  未写入的整场"。

# P0 零依赖

纯 dict / OrderedDict，无锁，靠 GIL 原子性。parser 解析完一条 LiveEvent 后
调一次 ``record_many()``，不阻塞主链路。
"""
from __future__ import annotations

from collections import OrderedDict

from polymarket_trader.domain.sports_live import SoccerMatchEvent

_Fingerprint = tuple[str, str, str, str, str]


def _fingerprint(event: SoccerMatchEvent) -> _Fingerprint:
    return (
        event.event_type,
        event.player_id,
        event.team,
        event.minute,
        event.score_after,
    )


class MatchEventHistoryBuffer:
    """每 condition_id 维护比赛事件 ``OrderedDict[fp → event]``，整场保留。

    内存上限：
    - 单场 ``max_events_per_market``（100）覆盖足球全事件类型极端值（goal
      ~10 + cards ~10 + subst ~12 + VAR ~3 = ~35，留 ~3x 余量）；
    - 全局 ``max_markets``（2000）：OrderedDict LRU evict 防极端市场数爆。
    - 单事件 ~250B（6 字段 + datetime），100 × 2000 = ~50MB 上限，实际 < 3MB。
    """

    def __init__(
        self,
        *,
        max_events_per_market: int = 100,
        max_markets: int = 2000,
    ) -> None:
        self._max_events_per_market = max_events_per_market
        self._max_markets = max_markets
        self._buffers: OrderedDict[
            str, OrderedDict[_Fingerprint, SoccerMatchEvent]
        ] = OrderedDict()

    def record_many(
        self, condition_id: str, events: tuple[SoccerMatchEvent, ...]
    ) -> int:
        """把 ``events`` 追加到 ``condition_id`` 对应 buffer；已存在的去重。

        返回本次新增条目数。fingerprint 见模块 docstring。
        """

        if not events:
            return 0
        bucket = self._buffers.get(condition_id)
        if bucket is None:
            bucket = OrderedDict()
            self._buffers[condition_id] = bucket
        else:
            # 写时刷新 LRU——最近被写入的市场最近活跃，evict 优先踢久未写入的。
            self._buffers.move_to_end(condition_id)
        added = 0
        for event in events:
            fp = _fingerprint(event)
            if fp in bucket:
                continue
            bucket[fp] = event
            added += 1
        # 单场超上限：从最旧丢，fingerprint 与事件同生同灭。
        while len(bucket) > self._max_events_per_market:
            bucket.popitem(last=False)
        # 全局市场数超上限：踢最久未写入的整场。
        while len(self._buffers) > self._max_markets:
            self._buffers.popitem(last=False)
        return added

    def events(self, condition_id: str) -> tuple[SoccerMatchEvent, ...]:
        """返回 ``condition_id`` 当前 buffer 内的所有事件，按写入顺序。

        读不调 ``move_to_end``——读操作不扰外层 LRU，evict 顺序保持"最久
        未写入"语义。调用方拿到独立 tuple 拷贝后可跨线程使用；
        ``SoccerMatchEvent`` 是 frozen dataclass，引用拷贝安全。
        """

        bucket = self._buffers.get(condition_id)
        if not bucket:
            return ()
        return tuple(bucket.values())

    def clear(self, condition_id: str) -> None:
        """市场 settled / prune 时由清理回调调用，drop 整场。"""

        self._buffers.pop(condition_id, None)

    def event_count(self, condition_id: str) -> int:
        bucket = self._buffers.get(condition_id)
        return 0 if bucket is None else len(bucket)

    def tracked_market_count(self) -> int:
        return len(self._buffers)

    def memory_footprint_estimate(self) -> dict[str, int]:
        """容量 + 实际 market 数 + 事件总数，operator 观测内存使用。"""

        total_events = sum(len(b) for b in self._buffers.values())
        return {
            "tracked_markets": len(self._buffers),
            "max_markets": self._max_markets,
            "total_events": total_events,
            "max_events_per_market": self._max_events_per_market,
        }


__all__ = ["MatchEventHistoryBuffer"]
