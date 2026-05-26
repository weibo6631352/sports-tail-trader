"""Goalserve 隐含概率时序 buffer——连续采样时序信号源。

inplay GZIP feed 每秒推一次赔率，metadata 抽取层算出 ``home_implied_prob``
/ ``over_implied_prob`` 等单点值；本 buffer 把这些单点串成时间序列，让下游
策略 / 数据分析能看"最近 N 秒赔率方向反转"、"波动率"、"突然暂停"。

# 写入策略：dedup-on-equal-last

不是每次 feed 推送都记——与上一条同 ``(market_type, side)`` 样本对比，若
``implied_prob`` / ``line`` / ``suspended`` 都没变就跳过。这样：

- 内存随真实变化频率走，赔率长时间不变的市场几乎不占空间；
- 分析侧从时间戳间隔自然推断"值在 X 秒内保持"；
- 1Hz feed × 6 个 (mt, side) × 100 个 market 的极端场景下，每场~30 条变化／
  120s 窗口 → 总样本 ~3000，~150B/条 = ~450KB（包 Python 对象 overhead），
  实际跑 < 5MB。

# 时间窗

120s 滚动（超龄 popleft）。比 orderbook 15s 长一个数量级——赔率反转/方向
信号要看 30-90s 跨度才稳定，太短抓不到大趋势。

# 数据结构

``OrderedDict[condition_id → (deque[GoalserveOddsSample], dict[(mt,side) → last])]``

- 外层 OrderedDict：LRU evict markets 上限
- 内层 deque：按 ts_monotonic 升序，超 120s 从左 popleft
- ``_last`` 字典：(market_type, side) → 最近一条样本，dedup-on-equal-last 用

# P0 零依赖

纯 dict / OrderedDict / deque，无锁，靠 GIL 原子性。match_service._apply_match
解析完 metadata 后同步调一次 ``record_metadata()``，~微秒级。
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.sports_live import (
    GoalserveOddsMarketType,
    GoalserveOddsSample,
    GoalserveOddsSide,
)

_DedupKey = tuple[GoalserveOddsMarketType, GoalserveOddsSide]


@dataclass(slots=True)
class _Sample:
    """内部包装：deque 元素带 ts_monotonic 做时间窗剪枝。"""

    ts_mono: float
    sample: GoalserveOddsSample


@dataclass(slots=True)
class _Bucket:
    samples: deque[_Sample]
    last: dict[_DedupKey, GoalserveOddsSample]


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError):
        return None


def _emit(
    market_type: GoalserveOddsMarketType,
    side: GoalserveOddsSide,
    prob_raw: object,
    line: Decimal | None,
    suspended: bool,
    observed_at: datetime,
) -> GoalserveOddsSample | None:
    prob = _decimal_or_none(prob_raw)
    if prob is None:
        return None
    return GoalserveOddsSample(
        market_type=market_type,
        side=side,
        implied_prob=prob,
        line=line,
        suspended=suspended,
        observed_at=observed_at,
    )


def extract_odds_samples(
    metadata: Mapping[str, Any], observed_at: datetime
) -> tuple[GoalserveOddsSample, ...]:
    """从 metadata 提取所有 Goalserve 赔率方向的当下样本（平铺）。

    覆盖 4 种 market_type：
    - ``goalserve_moneyline`` / ``goalserve_halftime``：home / away / draw（足球
      3-way 才有 draw_implied_prob）
    - ``goalserve_spread``：home / away，line = home_handicap
    - ``goalserve_totals``：over / under，line = total_line

    ``suspended`` 合并：盘口整体 ``suspended`` 或本方向 ``{side}_suspended``
    任一为 true 即视为该方向暂停。
    """

    out: list[GoalserveOddsSample] = []

    # moneyline + halftime + spread：home / away（spread 额外带 line）
    for meta_key, mt in (
        ("goalserve_moneyline", "moneyline"),
        ("goalserve_halftime", "halftime"),
        ("goalserve_spread", "spread"),
    ):
        odds = metadata.get(meta_key)
        if not isinstance(odds, Mapping):
            continue
        market_suspended = bool(odds.get("suspended"))
        line = _decimal_or_none(odds.get("home_handicap")) if mt == "spread" else None
        for side_label in ("home", "away"):
            sample = _emit(
                mt,  # type: ignore[arg-type]
                side_label,  # type: ignore[arg-type]
                odds.get(f"{side_label}_implied_prob"),
                line,
                market_suspended or bool(odds.get(f"{side_label}_suspended")),
                observed_at,
            )
            if sample is not None:
                out.append(sample)
        # 足球 3-way moneyline / halftime 含 draw
        if mt in ("moneyline", "halftime") and odds.get("draw_implied_prob") is not None:
            sample = _emit(
                mt,  # type: ignore[arg-type]
                "draw",
                odds.get("draw_implied_prob"),
                None,
                market_suspended or bool(odds.get("draw_suspended")),
                observed_at,
            )
            if sample is not None:
                out.append(sample)

    # totals：over / under + line=total_line
    totals = metadata.get("goalserve_totals")
    if isinstance(totals, Mapping):
        market_suspended = bool(totals.get("suspended"))
        line = _decimal_or_none(totals.get("total_line"))
        for side_label in ("over", "under"):
            sample = _emit(
                "totals",
                side_label,  # type: ignore[arg-type]
                totals.get(f"{side_label}_implied_prob"),
                line,
                market_suspended or bool(totals.get(f"{side_label}_suspended")),
                observed_at,
            )
            if sample is not None:
                out.append(sample)

    return tuple(out)


def _is_change(prev: GoalserveOddsSample, current: GoalserveOddsSample) -> bool:
    """同 ``(market_type, side)`` 下，``implied_prob`` / ``line`` / ``suspended``
    任一变化即视为新样本——否则跳过 dedup。"""

    return (
        prev.implied_prob != current.implied_prob
        or prev.line != current.line
        or prev.suspended != current.suspended
    )


class GoalserveOddsHistoryBuffer:
    """每 condition_id 维护 Goalserve 赔率样本时间序列。

    内存上限：
    - ``max_age_s``（120）滚动窗，超龄 popleft；
    - ``max_markets``（2000）：OrderedDict LRU evict 防极端市场数爆。
    """

    def __init__(
        self,
        *,
        max_age_s: float = 120.0,
        max_markets: int = 2000,
    ) -> None:
        self._max_age_s = max_age_s
        self._max_markets = max_markets
        self._buffers: OrderedDict[str, _Bucket] = OrderedDict()

    def record_metadata(
        self,
        condition_id: str,
        metadata: Mapping[str, Any],
        observed_at: datetime,
    ) -> int:
        """从 metadata 抽样并 dedup-on-equal-last 写入；返回本次新增条目数。"""

        return self.record_many(
            condition_id, extract_odds_samples(metadata, observed_at)
        )

    def record_many(
        self, condition_id: str, samples: tuple[GoalserveOddsSample, ...]
    ) -> int:
        """直接追加预抽样本（用于测试或 helper 之外的写入路径）。"""

        if not samples:
            return 0
        bucket = self._buffers.get(condition_id)
        now_mono = time.monotonic()
        if bucket is None:
            bucket = _Bucket(samples=deque(), last={})
            self._buffers[condition_id] = bucket
        else:
            self._buffers.move_to_end(condition_id)
        added = 0
        for sample in samples:
            key = (sample.market_type, sample.side)
            prev = bucket.last.get(key)
            if prev is not None and not _is_change(prev, sample):
                continue
            bucket.samples.append(_Sample(ts_mono=now_mono, sample=sample))
            bucket.last[key] = sample
            added += 1
        # 时间窗剪枝：超 max_age_s 从左 popleft。``_last`` 不跟着剪
        # ——dedup 始终对比"最近一条"，与是否在窗口内无关。
        cutoff = now_mono - self._max_age_s
        while bucket.samples and bucket.samples[0].ts_mono < cutoff:
            bucket.samples.popleft()
        # 全局市场数超上限：踢最久未写入的整桶。
        while len(self._buffers) > self._max_markets:
            self._buffers.popitem(last=False)
        return added

    def samples(self, condition_id: str) -> tuple[GoalserveOddsSample, ...]:
        """返回 ``condition_id`` 当前窗口内全部样本，按写入序（时间升序）。

        读不调 ``move_to_end``——读不扰外层 LRU。``GoalserveOddsSample`` 是
        frozen dataclass，引用拷贝跨线程安全。
        """

        bucket = self._buffers.get(condition_id)
        if bucket is None or not bucket.samples:
            return ()
        return tuple(s.sample for s in bucket.samples)

    def clear(self, condition_id: str) -> None:
        """市场 prune / settled 时由 lifecycle listener 调用，drop 整桶。"""

        self._buffers.pop(condition_id, None)

    def sample_count(self, condition_id: str) -> int:
        bucket = self._buffers.get(condition_id)
        return 0 if bucket is None else len(bucket.samples)

    def tracked_market_count(self) -> int:
        return len(self._buffers)

    def memory_footprint_estimate(self) -> dict[str, int]:
        """容量 + 实际 market 数 + 样本总数，operator 观测内存使用。"""

        total_samples = sum(len(b.samples) for b in self._buffers.values())
        return {
            "tracked_markets": len(self._buffers),
            "max_markets": self._max_markets,
            "total_samples": total_samples,
            "max_age_s": int(self._max_age_s),
        }


__all__ = [
    "GoalserveOddsHistoryBuffer",
    "extract_odds_samples",
]
