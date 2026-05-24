"""盘口派生指标 (Domain 层纯函数 + 不可变数据结构)。

每次 orderbook WS 推送时由 publisher 调用 ``compute_derived(snapshot, history)``
一次性算出所有可观测派生指标 (microprice / depth_imbalance / 滑点表 / 流动性评级
/ 15s 窗口波动 / 多窗口 delta / whale)，落入 ``OrderbookDerivedStore``。

admin endpoint 永远只读 store，O(1) 不再现场算。

设计:
- 纯函数 + frozen+slots dataclass: 无 IO/锁/外部依赖,可独立单测.
- 全 Decimal: 与 ``domain/`` 其它金额/价格保持一致.
- 接受 ``history_samples`` 通用迭代器: 解耦 ``OrderbookHistoryBuffer`` 内部 _Sample 类.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Sequence

from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel


# ---------- 公共常量 (与现有 admin_query/market.py 等口径一致) ----------

# whale 阈值: USDC ≥ $50 视为 informed/鲸鱼大单
WHALE_THRESHOLD_USDC: Decimal = Decimal("50")

# 滑点冲击模拟的目标量级 (USDC)
DEFAULT_IMPACT_TARGETS_USDC: tuple[Decimal, ...] = (
    Decimal("100"),
    Decimal("500"),
    Decimal("1000"),
)

# 反向: 给定滑点上限 (bps) 可吃多少 USDC
DEFAULT_FILLABLE_SLIPPAGE_BPS: tuple[Decimal, ...] = (
    Decimal("100"),    # 1%
    Decimal("500"),    # 5%
    Decimal("1000"),   # 10%
)

# windows delta 的窗口长度 (秒)
DEFAULT_WINDOWS_SECONDS: tuple[float, ...] = (2.0, 3.0, 5.0, 10.0)

# 边缘价 (Polymarket): bid 侧地板 $0.01, ask 侧天花板 $0.99
_BID_EDGE_PRICE: Decimal = Decimal("0.01")
_ASK_EDGE_PRICE: Decimal = Decimal("0.99")


# ---------- 不可变数据结构 ----------


@dataclass(frozen=True, slots=True)
class _PriceLevelOut:
    """归一化后的盘口档位 (含 USDC 折算)。"""
    price: Decimal
    size: Decimal
    usdc: Decimal


@dataclass(frozen=True, slots=True)
class PriceImpactRow:
    """模拟吃指定 USDC 量级时的 ask 滑点曲线一行。"""
    target_usdc: Decimal
    filled: bool
    # filled=False 时: fillable_usdc 表示全 ask 吃完最多能花多少
    fillable_usdc: Decimal | None
    # filled=True 时: 下列字段非空
    avg_price: Decimal | None
    worst_price: Decimal | None
    slippage_bps_vs_best_ask: Decimal | None


@dataclass(frozen=True, slots=True)
class FillableRow:
    """给定滑点上限可吃多少 USDC (反向冲击曲线一行)。"""
    slippage_bps_max: Decimal
    max_price: Decimal | None
    fillable_usdc: Decimal


@dataclass(frozen=True, slots=True)
class WindowDelta:
    """N 秒窗口内的盘口变化 (size/usdc/best/mid/whale)。"""
    window_s: float
    available: bool
    bid_total_size_delta: Decimal
    ask_total_size_delta: Decimal
    bid_total_usdc_delta: Decimal
    ask_total_usdc_delta: Decimal
    best_bid_delta: Decimal | None
    best_ask_delta: Decimal | None
    mid_delta: Decimal | None
    bid_whale_count_delta: int
    ask_whale_count_delta: int
    bid_whale_usdc_delta: Decimal
    ask_whale_usdc_delta: Decimal


@dataclass(frozen=True, slots=True)
class DirectionSnapshot:
    """OFI 风向信号的投影 (复用 ``OrderbookDeltaStore.direction_signal()`` 算法,
    嵌入 DerivedMetrics 让 admin 一站式可观测)。

    只保留归一化结果 + 标签 + 置信度; raw deltas/velocity 等细节通过
    ``/markets/orderbook-direction`` 独立 endpoint 仍可访问 (避免 DerivedMetrics
    payload 膨胀)。
    """
    window_seconds: float
    sample_count: int
    direction_score: Decimal       # [-1,+1] mid_delta 归一
    price_momentum: Decimal        # [-1,+1] mid_velocity 归一
    flow_imbalance: Decimal        # [-1,+1] real_depth 消耗失衡
    direction_label: str           # "yes" | "no" | "neutral"
    confidence: Decimal            # [0,1]


@dataclass(frozen=True, slots=True)
class SideSummary:
    """单侧 (bid 或 ask) 的聚合指标 + top 20 档 + whale 明细。"""
    total_size: Decimal
    total_usdc: Decimal
    level_count: int
    levels_top20: tuple[_PriceLevelOut, ...]
    median_price: Decimal | None
    median_size_cumulative: Decimal
    edge_price: Decimal
    edge_size: Decimal
    edge_usdc: Decimal
    whale_threshold_usdc: Decimal
    whale_count: int
    whale_total_usdc: Decimal
    whale_total_size: Decimal
    whale_levels: tuple[_PriceLevelOut, ...]


@dataclass(frozen=True, slots=True)
class DerivedMetrics:
    """单 token 在某一时刻 (computed_at) 的完整派生指标。

    所有字段在 ``compute_derived()`` 一次性算出 → 写入 ``OrderbookDerivedStore``;
    admin endpoint 与策略层后续只读, 不重算。
    """
    computed_at: datetime
    token_id: str

    # 基础: best / mid / spread
    best_bid: Decimal | None
    best_ask: Decimal | None
    best_bid_size: Decimal | None
    best_ask_size: Decimal | None
    mid: Decimal | None
    spread_abs: Decimal | None
    relative_spread_bps: Decimal | None

    # 隐含概率 (Polymarket 二元市场: bid=下限, ask=上限, mid=共识)
    implied_prob_lower: Decimal | None
    implied_prob_upper: Decimal | None
    implied_prob_mid: Decimal | None

    # microprice (size 加权下一笔成交价) + 与 mid 的偏离
    microprice: Decimal | None
    microprice_divergence_bps: Decimal | None

    # quoted depth at best (best 一档双边)
    quoted_depth_at_best_bid_size: Decimal
    quoted_depth_at_best_ask_size: Decimal
    quoted_depth_at_best_bid_usdc: Decimal
    quoted_depth_at_best_ask_usdc: Decimal
    quoted_depth_at_best_total_size: Decimal

    # depth imbalance: (bb_size - ba_size) / (bb_size + ba_size), [-1,+1]
    depth_imbalance: Decimal | None

    # 滑点冲击曲线
    price_impact: tuple[PriceImpactRow, ...]
    fillable_within_slippage: tuple[FillableRow, ...]

    # 流动性评级 (综合 0-1) + 等级标签
    liquidity_score: Decimal
    liquidity_tier: str  # high / medium / low / dead

    # 15s 窗口波动 (来自 history_samples)
    sample_count_15s: int
    quote_update_rate_per_s: Decimal | None
    mid_volatility_15s: Decimal | None
    mid_cv_15s_bps: Decimal | None
    mid_direction_reversals_15s: int
    mid_change_total_15s: Decimal | None
    spread_volatility_15s: Decimal | None
    spread_max_15s: Decimal | None
    spread_min_15s: Decimal | None
    spread_avg_15s: Decimal | None
    resilience_score: Decimal  # 综合稳定度 0-1

    # 双边深度聚合 + whale
    bid_side: SideSummary
    ask_side: SideSummary

    # 多窗口 delta (默认 2/3/5/10s)
    windows: tuple[WindowDelta, ...]

    # OFI 风向信号投影 (publisher 从 OrderbookDeltaStore.direction_signal() 拼装),
    # sample 不足 (启动早期 / token 首次推送) 时为 None.
    # 嵌入此处让 admin /markets/orderbook-depth 一站式拿到所有可观测信号.
    direction: DirectionSnapshot | None


# ---------- 内部计算辅助 ----------


def _to_level_out(level: PriceLevel) -> _PriceLevelOut:
    return _PriceLevelOut(
        price=level.price,
        size=level.size,
        usdc=level.size * level.price,
    )


def _whale_aggregate(levels: Sequence[PriceLevel]) -> tuple[int, Decimal, Decimal, list[_PriceLevelOut]]:
    """统计 USDC ≥ WHALE_THRESHOLD_USDC 的大单 (count / total_usdc / total_size / 明细)."""
    count = 0
    total_usdc = Decimal("0")
    total_size = Decimal("0")
    detail: list[_PriceLevelOut] = []
    for lvl in levels:
        usdc = lvl.size * lvl.price
        if usdc >= WHALE_THRESHOLD_USDC:
            count += 1
            total_usdc += usdc
            total_size += lvl.size
            detail.append(_PriceLevelOut(price=lvl.price, size=lvl.size, usdc=usdc))
    return count, total_usdc, total_size, detail


def _side_summary(levels: Sequence[PriceLevel], *, side: str) -> SideSummary:
    """按 best-first 排序后聚合: total / median / edge / whale / top 20。"""
    if not levels:
        edge_price = _BID_EDGE_PRICE if side == "bid" else _ASK_EDGE_PRICE
        return SideSummary(
            total_size=Decimal("0"),
            total_usdc=Decimal("0"),
            level_count=0,
            levels_top20=(),
            median_price=None,
            median_size_cumulative=Decimal("0"),
            edge_price=edge_price,
            edge_size=Decimal("0"),
            edge_usdc=Decimal("0"),
            whale_threshold_usdc=WHALE_THRESHOLD_USDC,
            whale_count=0,
            whale_total_usdc=Decimal("0"),
            whale_total_size=Decimal("0"),
            whale_levels=(),
        )

    # bid 价格高=好 (desc); ask 价格低=好 (asc)
    if side == "bid":
        sorted_levels = sorted(levels, key=lambda lvl: lvl.price, reverse=True)
    else:
        sorted_levels = sorted(levels, key=lambda lvl: lvl.price)

    total_size = sum((lvl.size for lvl in sorted_levels), Decimal("0"))
    total_usdc = sum((lvl.size * lvl.price for lvl in sorted_levels), Decimal("0"))

    # 中位价: 从 best 端累积 size 到 total/2
    target = total_size / 2
    cum = Decimal("0")
    median_price: Decimal | None = None
    for lvl in sorted_levels:
        cum += lvl.size
        if cum >= target:
            median_price = lvl.price
            break

    # 边缘价上挂单 (bid $0.01 / ask $0.99): MM 兜底单识别
    edge_price = _BID_EDGE_PRICE if side == "bid" else _ASK_EDGE_PRICE
    edge_size = Decimal("0")
    for lvl in sorted_levels:
        if lvl.price == edge_price:
            edge_size = lvl.size
            break
    edge_usdc = edge_size * edge_price

    whale_count, whale_total_usdc, whale_total_size, whale_levels = _whale_aggregate(sorted_levels)

    top_levels = tuple(_to_level_out(lvl) for lvl in sorted_levels[:20])

    return SideSummary(
        total_size=total_size,
        total_usdc=total_usdc,
        level_count=len(levels),
        levels_top20=top_levels,
        median_price=median_price,
        median_size_cumulative=cum,
        edge_price=edge_price,
        edge_size=edge_size,
        edge_usdc=edge_usdc,
        whale_threshold_usdc=WHALE_THRESHOLD_USDC,
        whale_count=whale_count,
        whale_total_usdc=whale_total_usdc,
        whale_total_size=whale_total_size,
        whale_levels=tuple(whale_levels),
    )


def _price_impact(asks_sorted: Sequence[PriceLevel], best_ask: Decimal | None) -> tuple[PriceImpactRow, ...]:
    """模拟逐档吃 X USDC 的 ask 滑点曲线 (3 个目标量级)."""
    rows: list[PriceImpactRow] = []
    for target_usdc in DEFAULT_IMPACT_TARGETS_USDC:
        spent = Decimal("0")
        shares = Decimal("0")
        last_price: Decimal | None = None
        for lvl in asks_sorted:
            lvl_value = lvl.size * lvl.price
            if spent + lvl_value >= target_usdc:
                remaining = target_usdc - spent
                partial_shares = remaining / lvl.price
                shares += partial_shares
                last_price = lvl.price
                spent = target_usdc
                break
            spent += lvl_value
            shares += lvl.size
            last_price = lvl.price
        if spent < target_usdc:
            rows.append(PriceImpactRow(
                target_usdc=target_usdc,
                filled=False,
                fillable_usdc=spent,
                avg_price=None,
                worst_price=None,
                slippage_bps_vs_best_ask=None,
            ))
            continue
        avg_price = spent / shares if shares > 0 else None
        slippage_bps: Decimal | None = None
        if avg_price is not None and best_ask is not None and best_ask > 0:
            slippage_bps = (avg_price - best_ask) / best_ask * Decimal("10000")
        rows.append(PriceImpactRow(
            target_usdc=target_usdc,
            filled=True,
            fillable_usdc=None,
            avg_price=avg_price,
            worst_price=last_price,
            slippage_bps_vs_best_ask=slippage_bps,
        ))
    return tuple(rows)


def _fillable_within(asks_sorted: Sequence[PriceLevel], best_ask: Decimal | None) -> tuple[FillableRow, ...]:
    """反向: 给定滑点上限可吃多少 USDC."""
    rows: list[FillableRow] = []
    for slip_bps in DEFAULT_FILLABLE_SLIPPAGE_BPS:
        if best_ask is None or best_ask <= 0:
            rows.append(FillableRow(slippage_bps_max=slip_bps, max_price=None, fillable_usdc=Decimal("0")))
            continue
        max_price = best_ask * (Decimal("1") + slip_bps / Decimal("10000"))
        total_usdc = Decimal("0")
        for lvl in asks_sorted:
            if lvl.price > max_price:
                break
            total_usdc += lvl.size * lvl.price
        rows.append(FillableRow(slippage_bps_max=slip_bps, max_price=max_price, fillable_usdc=total_usdc))
    return tuple(rows)


def _liquidity_score_and_tier(
    *,
    relative_spread_bps: Decimal | None,
    quoted_total_best_usdc: Decimal,
    depth_imbalance: Decimal | None,
) -> tuple[Decimal, str]:
    """综合分 0-1: 0.5×spread + 0.3×depth + 0.2×balance, 再映射 tier."""
    score = Decimal("0")
    # spread_score: <=100 bps 满分, >=500 bps 0 分
    if relative_spread_bps is not None:
        if relative_spread_bps <= Decimal("100"):
            score += Decimal("0.5")
        elif relative_spread_bps < Decimal("500"):
            score += Decimal("0.5") * (Decimal("500") - relative_spread_bps) / Decimal("400")
    # depth_score: best 双边 USDC >= $500 满分, 0 → 0
    if quoted_total_best_usdc >= Decimal("500"):
        score += Decimal("0.3")
    elif quoted_total_best_usdc > 0:
        score += Decimal("0.3") * (quoted_total_best_usdc / Decimal("500"))
    # balance_score: |depth_imbalance| <= 0.2 满分, >=0.8 0 分
    if depth_imbalance is not None:
        ab = abs(depth_imbalance)
        if ab <= Decimal("0.2"):
            score += Decimal("0.2")
        elif ab < Decimal("0.8"):
            score += Decimal("0.2") * (Decimal("0.8") - ab) / Decimal("0.6")
    if score >= Decimal("0.7"):
        tier = "high"
    elif score >= Decimal("0.4"):
        tier = "medium"
    elif score > 0:
        tier = "low"
    else:
        tier = "dead"
    return score, tier


def _std(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    # Decimal ** 0.5 不支持, 用 sqrt 兼容写法
    return var ** Decimal("0.5") if var > 0 else Decimal("0")


def _history_stats(history_samples: Sequence[tuple[float, OrderbookSnapshot]]) -> dict:
    """从 (ts_mono, snapshot) 序列算 15s 窗口波动指标 (内部 dict, 由调用方拆字段)."""
    n = len(history_samples)
    if n < 2:
        return {
            "sample_count_15s": n,
            "quote_update_rate_per_s": None,
            "mid_volatility_15s": None,
            "mid_cv_15s_bps": None,
            "mid_direction_reversals_15s": 0,
            "mid_change_total_15s": None,
            "spread_volatility_15s": None,
            "spread_max_15s": None,
            "spread_min_15s": None,
            "spread_avg_15s": None,
        }
    ts_first = history_samples[0][0]
    ts_last = history_samples[-1][0]
    duration_s = max(0.001, ts_last - ts_first)
    quote_rate = Decimal(str(round(n / duration_s, 2)))

    mids: list[Decimal] = []
    spreads: list[Decimal] = []
    for _ts, snap in history_samples:
        if snap.best_bid is not None and snap.best_ask is not None:
            mids.append((snap.best_bid + snap.best_ask) / Decimal("2"))
            spreads.append(snap.best_ask - snap.best_bid)

    mid_std = _std(mids) if mids else None
    mid_cv_bps: Decimal | None = None
    mid_change_total: Decimal | None = None
    direction_reversals = 0
    if mids and mid_std is not None:
        mid_mean = sum(mids) / len(mids)
        if mid_mean > 0:
            mid_cv_bps = mid_std / mid_mean * Decimal("10000")
        mid_change_total = mids[-1] - mids[0]
        for i in range(2, len(mids)):
            d1 = mids[i - 1] - mids[i - 2]
            d2 = mids[i] - mids[i - 1]
            if d1 * d2 < 0:
                direction_reversals += 1

    spread_std = _std(spreads) if spreads else None
    spread_max = max(spreads) if spreads else None
    spread_min = min(spreads) if spreads else None
    spread_avg = sum(spreads) / len(spreads) if spreads else None

    return {
        "sample_count_15s": n,
        "quote_update_rate_per_s": quote_rate,
        "mid_volatility_15s": mid_std,
        "mid_cv_15s_bps": mid_cv_bps,
        "mid_direction_reversals_15s": direction_reversals,
        "mid_change_total_15s": mid_change_total,
        "spread_volatility_15s": spread_std,
        "spread_max_15s": spread_max,
        "spread_min_15s": spread_min,
        "spread_avg_15s": spread_avg,
    }


def _resilience_score(
    *,
    spread_volatility_15s: Decimal | None,
    mid_cv_15s_bps: Decimal | None,
    quote_update_rate_per_s: Decimal | None,
) -> Decimal:
    """综合稳定度 0-1: 0.4×spread 稳 + 0.3×mid 稳 + 0.3×quote 活跃。"""
    score = Decimal("0")
    spr_std = spread_volatility_15s if spread_volatility_15s is not None else Decimal("0")
    mid_cv = mid_cv_15s_bps if mid_cv_15s_bps is not None else Decimal("0")
    qrate = quote_update_rate_per_s if quote_update_rate_per_s is not None else Decimal("0")
    if spr_std <= Decimal("0.005"):
        score += Decimal("0.4")
    elif spr_std < Decimal("0.05"):
        score += Decimal("0.4") * (Decimal("0.05") - spr_std) / Decimal("0.045")
    if mid_cv <= Decimal("20"):
        score += Decimal("0.3")
    elif mid_cv < Decimal("200"):
        score += Decimal("0.3") * (Decimal("200") - mid_cv) / Decimal("180")
    if qrate >= Decimal("5"):
        score += Decimal("0.3")
    elif qrate > Decimal("0.1"):
        score += Decimal("0.3") * (qrate - Decimal("0.1")) / Decimal("4.9")
    return score


def _windows_delta(
    *,
    current: OrderbookSnapshot,
    history_samples: Sequence[tuple[float, OrderbookSnapshot]],
    now_mono: float,
    windows_seconds: Sequence[float],
) -> tuple[WindowDelta, ...]:
    """在每个窗口长度上算盘口聚合的 delta (current - past)."""
    cur_bid_total = sum((lvl.size for lvl in current.bids), Decimal("0"))
    cur_ask_total = sum((lvl.size for lvl in current.asks), Decimal("0"))
    cur_bid_usdc = sum((lvl.size * lvl.price for lvl in current.bids), Decimal("0"))
    cur_ask_usdc = sum((lvl.size * lvl.price for lvl in current.asks), Decimal("0"))
    cur_mid: Decimal | None = None
    if current.best_bid is not None and current.best_ask is not None:
        cur_mid = (current.best_bid + current.best_ask) / Decimal("2")
    cur_bid_whale_count, cur_bid_whale_usdc, _, _ = _whale_aggregate(current.bids)
    cur_ask_whale_count, cur_ask_whale_usdc, _, _ = _whale_aggregate(current.asks)

    rows: list[WindowDelta] = []
    for w_seconds in windows_seconds:
        past = _find_snapshot_at(history_samples, now_mono=now_mono, ago_s=w_seconds)
        if past is None:
            rows.append(WindowDelta(
                window_s=w_seconds,
                available=False,
                bid_total_size_delta=Decimal("0"),
                ask_total_size_delta=Decimal("0"),
                bid_total_usdc_delta=Decimal("0"),
                ask_total_usdc_delta=Decimal("0"),
                best_bid_delta=None,
                best_ask_delta=None,
                mid_delta=None,
                bid_whale_count_delta=0,
                ask_whale_count_delta=0,
                bid_whale_usdc_delta=Decimal("0"),
                ask_whale_usdc_delta=Decimal("0"),
            ))
            continue
        past_bid_total = sum((lvl.size for lvl in past.bids), Decimal("0"))
        past_ask_total = sum((lvl.size for lvl in past.asks), Decimal("0"))
        past_bid_usdc = sum((lvl.size * lvl.price for lvl in past.bids), Decimal("0"))
        past_ask_usdc = sum((lvl.size * lvl.price for lvl in past.asks), Decimal("0"))
        past_mid: Decimal | None = None
        if past.best_bid is not None and past.best_ask is not None:
            past_mid = (past.best_bid + past.best_ask) / Decimal("2")
        past_bid_whale_count, past_bid_whale_usdc, _, _ = _whale_aggregate(past.bids)
        past_ask_whale_count, past_ask_whale_usdc, _, _ = _whale_aggregate(past.asks)
        rows.append(WindowDelta(
            window_s=w_seconds,
            available=True,
            bid_total_size_delta=cur_bid_total - past_bid_total,
            ask_total_size_delta=cur_ask_total - past_ask_total,
            bid_total_usdc_delta=cur_bid_usdc - past_bid_usdc,
            ask_total_usdc_delta=cur_ask_usdc - past_ask_usdc,
            best_bid_delta=(current.best_bid - past.best_bid)
                if (current.best_bid is not None and past.best_bid is not None) else None,
            best_ask_delta=(current.best_ask - past.best_ask)
                if (current.best_ask is not None and past.best_ask is not None) else None,
            mid_delta=(cur_mid - past_mid) if (cur_mid is not None and past_mid is not None) else None,
            bid_whale_count_delta=cur_bid_whale_count - past_bid_whale_count,
            ask_whale_count_delta=cur_ask_whale_count - past_ask_whale_count,
            bid_whale_usdc_delta=cur_bid_whale_usdc - past_bid_whale_usdc,
            ask_whale_usdc_delta=cur_ask_whale_usdc - past_ask_whale_usdc,
        ))
    return tuple(rows)


def _find_snapshot_at(
    history_samples: Sequence[tuple[float, OrderbookSnapshot]],
    *,
    now_mono: float,
    ago_s: float,
) -> OrderbookSnapshot | None:
    """在 (ts_mono, snapshot) 序列中找最接近 now-ago 的样本。"""
    if not history_samples:
        return None
    target = now_mono - ago_s
    best: OrderbookSnapshot | None = None
    best_diff = float("inf")
    for ts_mono, snap in history_samples:
        diff = abs(ts_mono - target)
        if diff < best_diff:
            best_diff = diff
            best = snap
        else:
            break  # 按 ts 升序后, diff 开始增长说明已经过了 target
    return best


# ---------- 唯一公开入口 ----------


def compute_derived(
    snapshot: OrderbookSnapshot,
    history_samples: Iterable[tuple[float, OrderbookSnapshot]] = (),
    *,
    direction: DirectionSnapshot | None = None,
    now_mono: float | None = None,
    windows_seconds: Sequence[float] = DEFAULT_WINDOWS_SECONDS,
    computed_at: datetime | None = None,
) -> DerivedMetrics:
    """一次性算出 ``snapshot`` 的所有派生指标。纯函数, 无 IO。

    Args:
        snapshot: 当前盘口快照 (由 market_ws 推送)。
        history_samples: 15s 窗口内的 (ts_mono, snapshot) 序列, 按 ts 升序。
            由 publisher 从 ``OrderbookHistoryBuffer`` 取出后传入。
        direction: 已算好的风向信号投影 (由 publisher 调用
            ``OrderbookDeltaStore.direction_signal()`` 拼装并传入)。
            sample 不足 / 启动早期 → None, 嵌入到 ``DerivedMetrics.direction``。
            分开传入 (而非内部调) 保持 ``compute_derived`` 是 domain 纯函数。
        now_mono: ``time.monotonic()`` 当前值, 用于 windows delta 找过去样本。
            默认用 history 最后一条 ts (或 0)。
        windows_seconds: 多窗口 delta 长度, 默认 (2,3,5,10)。
        computed_at: 显式时间戳 (用于测试可重复)。默认 datetime.now(UTC)。
    """
    samples_list = list(history_samples)
    if now_mono is None:
        now_mono = samples_list[-1][0] if samples_list else 0.0
    if computed_at is None:
        computed_at = datetime.now(timezone.utc)

    bb = snapshot.best_bid
    ba = snapshot.best_ask
    bbs = snapshot.best_bid_size if snapshot.best_bid_size is not None else Decimal("0")
    bas = snapshot.best_ask_size if snapshot.best_ask_size is not None else Decimal("0")

    # 基础: mid / spread / 隐含概率 / microprice
    mid: Decimal | None = None
    spread_abs: Decimal | None = None
    relative_spread_bps: Decimal | None = None
    implied_lower: Decimal | None = None
    implied_upper: Decimal | None = None
    implied_mid: Decimal | None = None
    microprice = snapshot.microprice
    microprice_div_bps: Decimal | None = None

    if bb is not None and ba is not None and bb > 0 and ba > 0:
        mid = (bb + ba) / Decimal("2")
        spread_abs = ba - bb
        if mid > 0:
            relative_spread_bps = spread_abs / mid * Decimal("10000")
        implied_lower = bb
        implied_upper = ba
        implied_mid = mid
        if microprice is not None and mid > 0:
            microprice_div_bps = (microprice - mid) / mid * Decimal("10000")

    # quoted depth at best
    qd_bid_usdc = bbs * bb if bb is not None else Decimal("0")
    qd_ask_usdc = bas * ba if ba is not None else Decimal("0")

    # depth imbalance
    total_best_size = bbs + bas
    depth_imbalance: Decimal | None = None
    if total_best_size > 0:
        depth_imbalance = (bbs - bas) / total_best_size

    # 滑点曲线 (按 ask 升序遍历)
    asks_sorted = sorted(snapshot.asks, key=lambda lvl: lvl.price) if snapshot.asks else ()
    price_impact = _price_impact(asks_sorted, ba)
    fillable = _fillable_within(asks_sorted, ba)

    # 流动性评级
    quoted_total_best_usdc = qd_bid_usdc + qd_ask_usdc
    liquidity_score, liquidity_tier = _liquidity_score_and_tier(
        relative_spread_bps=relative_spread_bps,
        quoted_total_best_usdc=quoted_total_best_usdc,
        depth_imbalance=depth_imbalance,
    )

    # 15s history stats + resilience
    history_stats = _history_stats(samples_list)
    resilience = _resilience_score(
        spread_volatility_15s=history_stats["spread_volatility_15s"],
        mid_cv_15s_bps=history_stats["mid_cv_15s_bps"],
        quote_update_rate_per_s=history_stats["quote_update_rate_per_s"],
    )

    # 双边聚合
    bid_side = _side_summary(snapshot.bids, side="bid")
    ask_side = _side_summary(snapshot.asks, side="ask")

    # 多窗口 delta
    windows = _windows_delta(
        current=snapshot,
        history_samples=samples_list,
        now_mono=now_mono,
        windows_seconds=windows_seconds,
    )

    return DerivedMetrics(
        computed_at=computed_at,
        token_id=snapshot.token_id,
        best_bid=bb,
        best_ask=ba,
        best_bid_size=snapshot.best_bid_size,
        best_ask_size=snapshot.best_ask_size,
        mid=mid,
        spread_abs=spread_abs,
        relative_spread_bps=relative_spread_bps,
        implied_prob_lower=implied_lower,
        implied_prob_upper=implied_upper,
        implied_prob_mid=implied_mid,
        microprice=microprice,
        microprice_divergence_bps=microprice_div_bps,
        quoted_depth_at_best_bid_size=bbs,
        quoted_depth_at_best_ask_size=bas,
        quoted_depth_at_best_bid_usdc=qd_bid_usdc,
        quoted_depth_at_best_ask_usdc=qd_ask_usdc,
        quoted_depth_at_best_total_size=total_best_size,
        depth_imbalance=depth_imbalance,
        price_impact=price_impact,
        fillable_within_slippage=fillable,
        liquidity_score=liquidity_score,
        liquidity_tier=liquidity_tier,
        sample_count_15s=history_stats["sample_count_15s"],
        quote_update_rate_per_s=history_stats["quote_update_rate_per_s"],
        mid_volatility_15s=history_stats["mid_volatility_15s"],
        mid_cv_15s_bps=history_stats["mid_cv_15s_bps"],
        mid_direction_reversals_15s=history_stats["mid_direction_reversals_15s"],
        mid_change_total_15s=history_stats["mid_change_total_15s"],
        spread_volatility_15s=history_stats["spread_volatility_15s"],
        spread_max_15s=history_stats["spread_max_15s"],
        spread_min_15s=history_stats["spread_min_15s"],
        spread_avg_15s=history_stats["spread_avg_15s"],
        resilience_score=resilience,
        bid_side=bid_side,
        ask_side=ask_side,
        windows=windows,
        direction=direction,
    )


__all__ = [
    "DerivedMetrics",
    "DirectionSnapshot",
    "PriceImpactRow",
    "FillableRow",
    "WindowDelta",
    "SideSummary",
    "compute_derived",
    "WHALE_THRESHOLD_USDC",
    "DEFAULT_IMPACT_TARGETS_USDC",
    "DEFAULT_FILLABLE_SLIPPAGE_BPS",
    "DEFAULT_WINDOWS_SECONDS",
]
