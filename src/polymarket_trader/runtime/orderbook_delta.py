from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from polymarket_trader.domain.orderbook import OrderbookSnapshot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class OrderbookSample:
    """单次盘口快照的微观结构字段。frozen dataclass 保证 deque 内的旧 sample
    不会被改写——并发读不需要锁(CPython deque append/iter atomic + frozen)。

    Polymarket 订单簿典型形态:两端集中(锁定价附近真单 + 远端 MM 地板单/天花板单)。
    用 best bid/ask + mid 做信号会被地板单严重失真。这里 store **microprice**(
    流动性加权 mid)+ **真实价区窗口内深度**,才是真公允锚点。
    """

    received_at: datetime
    best_bid: Decimal | None
    best_ask: Decimal | None
    best_bid_size: Decimal | None
    best_ask_size: Decimal | None
    # microprice = (bb × ask_size + ba × bid_size) / (bid_size + ask_size)
    # 真公允锚点,排除"算术 mid"在不对称深度下的失真。
    microprice: Decimal | None = None
    # 真实价区窗口内深度(microprice ± 0.05 内累积深度,排除地板/天花板单)。
    # 反映"如果想成交 $X 名义,实际能在多少价位吃到"。
    real_bid_depth_usdc: Decimal | None = None
    real_ask_depth_usdc: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderbookDirectionSignal:
    """窗口内 best bid/ask 的 price/size **变化 + 变化率 + 归一化方向**信号。

    单时点 bid/ask 深度比会被 MM 挂的远端"墙"骗,真买卖方向看 **best 价位移
    + size 消耗**。bid 抬升/ask 下移 = 买方在抬价(YES 方向);bid 下移/ask
    抬升 = 卖方在砸盘(NO 方向)。size 减少作为辅助置信度,可能是被吃也可能
    是撤单,单独不可靠。

    导出 3 个归一化 [-1,+1] 复合信号供策略消费:
    - direction_score: 价位移整体方向(mid_delta 归一)
    - price_momentum:  价位移速率(velocity 归一,反映抢/砸的力度)
    - flow_imbalance:  size 消耗失衡(ask 被吃多于 bid 被吃 = 买方主动 → 正)
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
    # microprice = 流动性加权 mid,排除 Polymarket 地板/天花板单失真。
    # 真公允锚点;mid_price_delta 优先用 microprice 算。
    first_microprice: Decimal | None
    last_microprice: Decimal | None
    microprice_delta: Decimal | None
    # 真实价区窗口内深度(microprice ± 0.05 USD 内),排除地板/天花板单。
    # 反映"实际能成交多少",不是被 MM 占位单虚高的算术深度。
    first_real_bid_depth_usdc: Decimal | None
    last_real_bid_depth_usdc: Decimal | None
    first_real_ask_depth_usdc: Decimal | None
    last_real_ask_depth_usdc: Decimal | None
    real_bid_depth_delta_usdc: Decimal | None
    real_ask_depth_delta_usdc: Decimal | None
    # 变化率(per second): delta / actual_window_seconds(用真实首末时差)。
    # velocity 单位:price/sec or size/sec。归一化版本在 _momentum / _flow 字段。
    bid_price_velocity: Decimal | None  # price/sec, 正=bid 上抬
    ask_price_velocity: Decimal | None  # price/sec, 正=ask 抬升
    mid_price_velocity: Decimal | None  # price/sec, 基于 microprice
    bid_size_consumption_rate: Decimal | None  # size/sec, 正=被吃(size 减少)
    ask_size_consumption_rate: Decimal | None
    # 归一化复合信号 [-1,+1] (正=YES,负=NO)
    direction_score: Decimal      # 基于 mid_delta 静态归一
    price_momentum: Decimal       # 基于 mid_velocity 归一(price 移动速率)
    flow_imbalance: Decimal       # ask 消耗率 - bid 消耗率 归一(订单流向)
    direction_label: str          # "yes" | "no" | "neutral"(综合 score+momentum+flow)
    confidence: Decimal           # [0, 1]

    def as_metadata(self) -> dict[str, Any]:
        """序列化成 dict 注入 DecisionContext.metadata['orderbook_direction']。

        策略只消费归一化复合信号 + label + confidence；不暴露 raw deltas
        （留给 operator 审计 endpoint）。Decimal 转 str 以保证 JSON 安全。
        """
        return {
            "window_seconds": float(self.window_seconds),
            "sample_count": self.sample_count,
            "direction_score": str(self.direction_score),
            "price_momentum": str(self.price_momentum),
            "flow_imbalance": str(self.flow_imbalance),
            "direction_label": self.direction_label,
            "confidence": str(self.confidence),
        }


# direction_score 用 mid_delta 归一: 每 0.005 价格波动算 1 个标准单位,
# 5 个标准单位(=0.025 = 2.5 个百分点)封顶。Polymarket tick=0.01,
# 这个区间覆盖大部分实盘场景。
_PRICE_DELTA_SCALE = Decimal("0.005")
_PRICE_DELTA_SATURATE_UNITS = Decimal("5")
# price_momentum 用 mid velocity 归一: 0.001 price/sec(每秒 10bp)算 1 个标准单位,
# 5 个单位(0.005 price/sec = 50bp/sec)封顶。极速波动场景。
_PRICE_VELOCITY_SCALE = Decimal("0.001")
_PRICE_VELOCITY_SATURATE_UNITS = Decimal("5")
# flow_imbalance 用 (ask_consumption - bid_consumption) / total_consumption 算,
# 已经在 [-1,+1] 区间,无需额外归一。total=0 时返回 0(无流动信号)。
# 单边方向 label 阈值: |score| >= 0.2 才视为有方向。
_DIRECTION_LABEL_THRESHOLD = Decimal("0.2")


class OrderbookDeltaStore:
    """per-token 盘口 best 价位环形缓冲 + 方向信号计算。

    - observe(snapshot): P0 路径上由 market_ws worker 在每次 snapshot 更新时
      调用,sync only,无 await/IO/lock(deque append CPython atomic)。
    - direction_signal(token_id, window_seconds): 读窗口内的 first/last sample
      算 delta + score,供策略 / operator 查询。
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
        # microprice 复用 OrderbookSnapshot.microprice @property（domain 层）——
        # 它已经识别 NO_BID / CEILING_ONLY / NO_ASK / FLOOR_ONLY 边界并返回
        # None，与"算术加权"的旧 _microprice 函数计算公式一致但更严谨。
        microprice = snapshot.microprice
        real_bid, real_ask = _real_price_band_depth(snapshot, microprice)
        sample = OrderbookSample(
            received_at=snapshot.received_at,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            best_bid_size=snapshot.best_bid_size,
            best_ask_size=snapshot.best_ask_size,
            microprice=microprice,
            real_bid_depth_usdc=real_bid,
            real_ask_depth_usdc=real_ask,
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
        and a.microprice == b.microprice
        and a.real_bid_depth_usdc == b.real_bid_depth_usdc
        and a.real_ask_depth_usdc == b.real_ask_depth_usdc
    )


# 真实价区窗口宽度(microprice 两侧各扩 5 个百分点)。超出此区间的挂单
# 视为 MM 地板单/天花板单,不参与"实际可成交深度"统计。
_REAL_PRICE_BAND = Decimal("0.05")


def _real_price_band_depth(
    snapshot: OrderbookSnapshot,
    microprice: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    """返回 microprice ± _REAL_PRICE_BAND 区间内 bid/ask 名义额(=price × size 之和)。

    排除 Polymarket 典型的"地板单"($0.001 230k)/"天花板单"($0.999 大量),
    这些挂单永远不会真正成交,但会把算术深度严重虚高。窗口内深度才是
    "现在如果想吃 $X,实际能成交多少"的有效指标。
    """

    if microprice is None:
        return None, None
    low = microprice - _REAL_PRICE_BAND
    high = microprice + _REAL_PRICE_BAND
    bid_depth = Decimal("0")
    for level in snapshot.bids:
        if level.price >= low and level.price <= high:
            bid_depth += level.price * level.size
    ask_depth = Decimal("0")
    for level in snapshot.asks:
        if level.price >= low and level.price <= high:
            ask_depth += level.price * level.size
    return bid_depth.quantize(Decimal("0.01")), ask_depth.quantize(Decimal("0.01"))


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
    # mid_delta 用 microprice 优先 — Polymarket 地板/天花板单会让算术 mid_delta 失真。
    # microprice 缺失才退回算术 mid(罕见,只在 best size 缺失时)。
    micro_delta = _delta(first.microprice, last.microprice)
    if micro_delta is not None:
        mid_delta: Decimal | None = micro_delta
    elif bid_delta is not None and ask_delta is not None:
        mid_delta = (bid_delta + ask_delta) / Decimal("2")
    elif bid_delta is not None:
        mid_delta = bid_delta
    elif ask_delta is not None:
        mid_delta = ask_delta
    else:
        mid_delta = None
    bid_size_delta = _delta(first.best_bid_size, last.best_bid_size)
    ask_size_delta = _delta(first.best_ask_size, last.best_ask_size)

    # 实际窗口时长用首末 sample 时差(不是 window_seconds 参数,因为后者是上限)。
    elapsed_seconds = (last.received_at - first.received_at).total_seconds()
    if elapsed_seconds <= 0:
        elapsed_seconds = 1.0  # 防除零(同时刻多 sample,velocity 退化为 delta)
    elapsed_dec = Decimal(str(elapsed_seconds))

    def _velocity(d: Decimal | None) -> Decimal | None:
        return None if d is None else (d / elapsed_dec).quantize(Decimal("0.000001"))

    bid_price_velocity = _velocity(bid_delta)
    ask_price_velocity = _velocity(ask_delta)
    mid_price_velocity = _velocity(mid_delta)
    # consumption_rate: size 减少为正(被吃),增加为负(MM 加单)。
    bid_consumption_rate = _velocity(-bid_size_delta) if bid_size_delta is not None else None
    ask_consumption_rate = _velocity(-ask_size_delta) if ask_size_delta is not None else None

    score = _direction_score(mid_delta)
    momentum = _price_momentum(mid_price_velocity)
    # flow_imbalance 用 real_depth_delta 算(L 下面),先占位,_compute_signal 末尾覆盖。
    flow = Decimal("0")
    label = "neutral"

    confidence = _confidence(arr)

    real_bid_delta = _delta(first.real_bid_depth_usdc, last.real_bid_depth_usdc)
    real_ask_delta = _delta(first.real_ask_depth_usdc, last.real_ask_depth_usdc)

    # 重新算 flow_imbalance: 用 real_depth_delta 而不是 best_size_delta。
    # best_price 变化时 best_size_delta 比较的是"不同价位的 size",无意义。
    # real_depth_delta 是 microprice ± 0.05 窗口内累积深度变化,排除地板单 + 始终
    # 对齐"真实价区",才是真的订单流向信号。
    # bid_depth 减少 → 真接盘被吃 → 卖压 (NO 方向);ask_depth 减少 → 真卖盘被吃 → 买压 (YES 方向)
    flow = _flow_imbalance_from_real_depth(real_bid_delta, real_ask_delta)

    # 综合 label: 三个信号 majority vote。
    label = _composite_label(score, momentum, flow)

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
        first_microprice=first.microprice,
        last_microprice=last.microprice,
        microprice_delta=micro_delta,
        first_real_bid_depth_usdc=first.real_bid_depth_usdc,
        last_real_bid_depth_usdc=last.real_bid_depth_usdc,
        first_real_ask_depth_usdc=first.real_ask_depth_usdc,
        last_real_ask_depth_usdc=last.real_ask_depth_usdc,
        real_bid_depth_delta_usdc=real_bid_delta,
        real_ask_depth_delta_usdc=real_ask_delta,
        bid_price_velocity=bid_price_velocity,
        ask_price_velocity=ask_price_velocity,
        mid_price_velocity=mid_price_velocity,
        bid_size_consumption_rate=bid_consumption_rate,
        ask_size_consumption_rate=ask_consumption_rate,
        direction_score=score,
        price_momentum=momentum,
        flow_imbalance=flow,
        direction_label=label,
        confidence=confidence,
    )


def _price_momentum(mid_velocity: Decimal | None) -> Decimal:
    """归一化 mid 价位移速率到 [-1, +1]。"""

    if mid_velocity is None or mid_velocity == 0:
        return Decimal("0")
    units = mid_velocity / _PRICE_VELOCITY_SCALE
    if units > _PRICE_VELOCITY_SATURATE_UNITS:
        units = _PRICE_VELOCITY_SATURATE_UNITS
    elif units < -_PRICE_VELOCITY_SATURATE_UNITS:
        units = -_PRICE_VELOCITY_SATURATE_UNITS
    return (units / _PRICE_VELOCITY_SATURATE_UNITS).quantize(Decimal("0.0001"))


def _flow_imbalance_from_real_depth(
    real_bid_delta: Decimal | None,
    real_ask_delta: Decimal | None,
) -> Decimal:
    """基于"真实价区窗口内深度变化"算订单流失衡。

    比 best size delta 可靠: best_price 变化时 best_size_delta 比较的是不同价位
    的 size,无意义;real_depth 是 microprice ± 0.05 内累积深度,始终对齐真实价区。

    - real_bid_depth 减少(delta < 0)= 真接盘被吃 = 卖压主导 → -1
    - real_ask_depth 减少(delta < 0)= 真卖盘被吃 = 买压主导 → +1
    归一: (-bid_delta + ask_delta) / (|bid_delta| + |ask_delta|)
        消耗 ask(正贡献)+ 消耗 bid(负贡献)。两者同符号变化(都加 / 都减)结果接近 0。
    """

    b = real_bid_delta or Decimal("0")
    a = real_ask_delta or Decimal("0")
    bid_consumed = -b  # bid 减少时 bid_consumed > 0
    ask_consumed = -a  # ask 减少时 ask_consumed > 0
    denom = abs(bid_consumed) + abs(ask_consumed)
    if denom == 0:
        return Decimal("0")
    return ((ask_consumed - bid_consumed) / denom).quantize(Decimal("0.0001"))


def _composite_label(score: Decimal, momentum: Decimal, flow: Decimal) -> str:
    """三个信号有任一显著(>=阈值)即拍板,majority vote(同向才认)。

    避免单一指标毛刺导致误判: 至少 2 个信号同向且其中一个超阈值。
    """

    th = _DIRECTION_LABEL_THRESHOLD
    yes_votes = sum(1 for v in (score, momentum, flow) if v >= th)
    no_votes = sum(1 for v in (score, momentum, flow) if v <= -th)
    if yes_votes >= 2:
        return "yes"
    if no_votes >= 2:
        return "no"
    # 单信号显著也认(防止 sample 少导致 momentum/flow 为 0 时漏报方向)
    if score >= th and no_votes == 0:
        return "yes"
    if score <= -th and yes_votes == 0:
        return "no"
    return "neutral"


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
