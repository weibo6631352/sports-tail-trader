from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from polymarket_trader.domain.orderbook import OrderbookSnapshot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class OrderbookSample:
    """单次盘口快照的最小信号字段。用 frozen dataclass 保证 deque 内的旧 sample
    不会被改写——并发读不需要锁(CPython deque append/iter atomic + frozen)。"""

    received_at: datetime
    best_bid: Decimal | None
    best_ask: Decimal | None
    best_bid_size: Decimal | None
    best_ask_size: Decimal | None


@dataclass(frozen=True, slots=True)
class OrderbookDirectionSignal:
    """窗口内 best bid/ask 的 price 和 size 变化。

    单时点 bid/ask 深度比会被 MM 挂的远端"墙"骗,真买卖方向看 **best 价位移
    + size 消耗**。bid 抬升/ask 下移 = 买方在抬价(YES 方向);bid 下移/ask
    抬升 = 卖方在砸盘(NO 方向)。size 减少作为辅助置信度,可能是被吃也可能
    是撤单,单独不可靠。
    """

    token_id: str
    window_seconds: float
    sample_count: int
    first_observed_at: datetime
    last_observed_at: datetime
    # raw deltas: positive = bid/ask price moved UP (YES direction strengthening)
    bid_price_delta: Decimal | None
    ask_price_delta: Decimal | None
    mid_price_delta: Decimal | None
    bid_size_delta: Decimal | None  # 正=挂单变多(MM 加单),负=被吃/被撤
    ask_size_delta: Decimal | None
    # normalized score: 综合 bid+ask price 移动,size 仅作辅助置信度。
    # [-1, +1],正=YES 方向(看涨),负=NO 方向(看跌)。
    direction_score: Decimal
    direction_label: str  # "yes" | "no" | "neutral"
    confidence: Decimal  # [0, 1],sample_count 越多 + 总深度越稳定置信度越高


# direction_score 用 mid_delta 归一: 每 0.005 价格波动算 1 个标准单位,
# 5 个标准单位(=0.025 = 2.5 个百分点)封顶。Polymarket tick=0.01,
# 这个区间覆盖大部分实盘场景。
_PRICE_DELTA_SCALE = Decimal("0.005")
_PRICE_DELTA_SATURATE_UNITS = Decimal("5")
# 单边方向 label 阈值: |score| >= 0.2 才视为有方向。
_DIRECTION_LABEL_THRESHOLD = Decimal("0.2")


class OrderbookDeltaStore:
    """per-token 盘口 best 价位环形缓冲 + 方向信号计算。

    - observe(snapshot): P0 路径上由 market_ws worker 在每次 snapshot 更新时
      调用,sync only,无 await/IO/lock(deque append CPython atomic)。
    - direction_signal(token_id, window_seconds): 读窗口内的 first/last sample
      算 delta + score,供策略 / admin 查询。
    """

    def __init__(
        self,
        *,
        max_window_seconds: float = 120.0,
        max_samples_per_token: int = 256,
    ) -> None:
        self._max_window = timedelta(seconds=max(1.0, max_window_seconds))
        self._max_samples = max(2, max_samples_per_token)
        self._samples: dict[str, deque[OrderbookSample]] = {}

    def observe(self, snapshot: OrderbookSnapshot) -> None:
        token_id = snapshot.token_id
        if not token_id:
            return
        sample = OrderbookSample(
            received_at=snapshot.received_at,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            best_bid_size=snapshot.best_bid_size,
            best_ask_size=snapshot.best_ask_size,
        )
        buf = self._samples.get(token_id)
        if buf is None:
            buf = deque(maxlen=self._max_samples)
            self._samples[token_id] = buf
        # 跳过完全重复的 sample(WS 心跳重发或快照无变化时常见),避免缓冲被
        # 同值塞满后窗口里全是一个时间点的拷贝。
        if buf and _samples_equal(buf[-1], sample):
            return
        buf.append(sample)

    def tracked_tokens(self) -> tuple[str, ...]:
        return tuple(self._samples.keys())

    def samples(self, token_id: str) -> tuple[OrderbookSample, ...]:
        buf = self._samples.get(token_id)
        return tuple(buf) if buf else ()

    def direction_signal(
        self,
        token_id: str,
        *,
        window_seconds: float = 10.0,
        now: datetime | None = None,
    ) -> OrderbookDirectionSignal | None:
        buf = self._samples.get(token_id)
        if not buf or len(buf) < 2:
            return None
        now_ts = now or _utc_now()
        window = timedelta(seconds=max(0.5, min(window_seconds, self._max_window.total_seconds())))
        cutoff = now_ts - window
        window_samples = tuple(s for s in buf if s.received_at >= cutoff)
        if len(window_samples) < 2:
            return None
        return _compute_signal(
            token_id=token_id,
            samples=window_samples,
            window_seconds=window.total_seconds(),
        )

    def prune(self, *, now: datetime | None = None) -> int:
        """删除超出 max_window 的旧 sample,返回删除条数。供周期清理。"""

        now_ts = now or _utc_now()
        cutoff = now_ts - self._max_window
        removed = 0
        for buf in self._samples.values():
            while buf and buf[0].received_at < cutoff:
                buf.popleft()
                removed += 1
        return removed


def _samples_equal(a: OrderbookSample, b: OrderbookSample) -> bool:
    return (
        a.best_bid == b.best_bid
        and a.best_ask == b.best_ask
        and a.best_bid_size == b.best_bid_size
        and a.best_ask_size == b.best_ask_size
    )


def _delta(first: Decimal | None, last: Decimal | None) -> Decimal | None:
    if first is None or last is None:
        return None
    return last - first


def _compute_signal(
    *,
    token_id: str,
    samples: Iterable[OrderbookSample],
    window_seconds: float,
) -> OrderbookDirectionSignal:
    arr = tuple(samples)
    first = arr[0]
    last = arr[-1]
    bid_delta = _delta(first.best_bid, last.best_bid)
    ask_delta = _delta(first.best_ask, last.best_ask)
    if bid_delta is not None and ask_delta is not None:
        mid_delta = (bid_delta + ask_delta) / Decimal("2")
    elif bid_delta is not None:
        mid_delta = bid_delta
    elif ask_delta is not None:
        mid_delta = ask_delta
    else:
        mid_delta = None
    bid_size_delta = _delta(first.best_bid_size, last.best_bid_size)
    ask_size_delta = _delta(first.best_ask_size, last.best_ask_size)

    score = _direction_score(mid_delta)
    if score >= _DIRECTION_LABEL_THRESHOLD:
        label = "yes"
    elif score <= -_DIRECTION_LABEL_THRESHOLD:
        label = "no"
    else:
        label = "neutral"

    confidence = _confidence(arr)

    return OrderbookDirectionSignal(
        token_id=token_id,
        window_seconds=window_seconds,
        sample_count=len(arr),
        first_observed_at=first.received_at,
        last_observed_at=last.received_at,
        bid_price_delta=bid_delta,
        ask_price_delta=ask_delta,
        mid_price_delta=mid_delta,
        bid_size_delta=bid_size_delta,
        ask_size_delta=ask_size_delta,
        direction_score=score,
        direction_label=label,
        confidence=confidence,
    )


def _direction_score(mid_delta: Decimal | None) -> Decimal:
    """把 mid 价位移归一到 [-1, +1]。每 _PRICE_DELTA_SCALE 算一个单位,
    _PRICE_DELTA_SATURATE_UNITS 个单位封顶。"""

    if mid_delta is None or mid_delta == 0:
        return Decimal("0")
    units = mid_delta / _PRICE_DELTA_SCALE
    if units > _PRICE_DELTA_SATURATE_UNITS:
        units = _PRICE_DELTA_SATURATE_UNITS
    elif units < -_PRICE_DELTA_SATURATE_UNITS:
        units = -_PRICE_DELTA_SATURATE_UNITS
    return (units / _PRICE_DELTA_SATURATE_UNITS).quantize(Decimal("0.0001"))


def _confidence(samples: tuple[OrderbookSample, ...]) -> Decimal:
    """sample 数越多 + best price 都齐全(非 None)→ 置信度越高。

    极简: sample_count/10 封顶 1.0,best_bid/ask 任一全程缺失 → 折半。
    策略侧可根据自己阈值再加工。
    """

    base = Decimal(min(len(samples), 10)) / Decimal("10")
    has_bid = any(s.best_bid is not None for s in samples)
    has_ask = any(s.best_ask is not None for s in samples)
    if not (has_bid and has_ask):
        base = base / Decimal("2")
    return base.quantize(Decimal("0.01"))


__all__ = [
    "OrderbookSample",
    "OrderbookDirectionSignal",
    "OrderbookDeltaStore",
]
