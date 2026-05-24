"""``compute_derived`` 纯函数行为约束 (无 IO/锁/外部依赖)。

覆盖:
- 基础: best/spread/microprice/depth_imbalance
- 边界: 空盘口 / 单侧空 / 缺 best size
- 滑点: price_impact 部分 fill / 全 fill / 不能 fill
- fillable_within_slippage: 各档遍历直到价位上限
- whale 阈值 = $50
- 15s history: sample_count / quote_rate / mid_volatility / direction_reversals
- windows delta: 找过去样本 + delta 计算
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal


from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.orderbook_derived import (
    DEFAULT_WINDOWS_SECONDS,
    WHALE_THRESHOLD_USDC,
    DirectionSnapshot,
    compute_derived,
)


# ---------- helpers ----------


def _ob(
    *,
    token_id: str = "tok",
    best_bid: str | None = "0.40",
    best_ask: str | None = "0.42",
    best_bid_size: str | None = "100",
    best_ask_size: str | None = "80",
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    received_at: datetime | None = None,
) -> OrderbookSnapshot:
    if bids is None:
        bids = [("0.40", "100"), ("0.39", "200"), ("0.30", "500")]
    if asks is None:
        asks = [("0.42", "80"), ("0.45", "300"), ("0.50", "500")]
    return OrderbookSnapshot(
        token_id=token_id,
        best_bid=Decimal(best_bid) if best_bid is not None else None,
        best_ask=Decimal(best_ask) if best_ask is not None else None,
        best_bid_size=Decimal(best_bid_size) if best_bid_size is not None else None,
        best_ask_size=Decimal(best_ask_size) if best_ask_size is not None else None,
        bids=tuple(PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids),
        asks=tuple(PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks),
        received_at=received_at or datetime(2026, 5, 24, 13, 0, 0, tzinfo=timezone.utc),
    )


# ---------- basic ----------


def test_compute_derived_basic_metrics() -> None:
    """有双边真单时 mid/spread/relative_spread/microprice 都算出且符号合理。"""
    snap = _ob()
    derived = compute_derived(snap)
    assert derived.token_id == "tok"
    assert derived.best_bid == Decimal("0.40")
    assert derived.best_ask == Decimal("0.42")
    assert derived.mid == Decimal("0.41")
    assert derived.spread_abs == Decimal("0.02")
    assert derived.relative_spread_bps is not None
    # 200 / 0.41 * 10000 = 487.8 bps
    assert Decimal("480") < derived.relative_spread_bps < Decimal("490")
    # microprice = (0.42*100 + 0.40*80) / (100+80) = (42+32)/180 = 0.4111...
    assert derived.microprice is not None
    assert Decimal("0.41") < derived.microprice < Decimal("0.42")
    # depth_imbalance = (100-80)/(100+80) = 0.111... > 0 (bid 厚)
    assert derived.depth_imbalance is not None
    assert derived.depth_imbalance > 0


def test_compute_derived_empty_orderbook() -> None:
    """完全空盘口: best/mid/microprice 都 None, side summary 空, 滑点不可填。"""
    snap = _ob(best_bid=None, best_ask=None, best_bid_size=None, best_ask_size=None,
               bids=[], asks=[])
    derived = compute_derived(snap)
    assert derived.best_bid is None
    assert derived.best_ask is None
    assert derived.mid is None
    assert derived.microprice is None
    assert derived.depth_imbalance is None
    assert derived.bid_side.level_count == 0
    assert derived.ask_side.level_count == 0
    assert derived.liquidity_tier == "dead"
    for impact in derived.price_impact:
        assert impact.filled is False
    for fill in derived.fillable_within_slippage:
        assert fill.fillable_usdc == Decimal("0")


def test_compute_derived_single_sided_book_no_ask() -> None:
    """只有 bid 侧 (ask 空): mid None, microprice 退化到 best_bid (snapshot 自身处理)。"""
    snap = _ob(best_ask=None, best_ask_size=None, asks=[])
    derived = compute_derived(snap)
    assert derived.mid is None
    assert derived.depth_imbalance is not None  # bbs=100, bas=0 → 1.0 (纯 bid)
    assert derived.depth_imbalance == Decimal("1")
    # ask 空 → price_impact 全部 not filled
    assert all(p.filled is False for p in derived.price_impact)


# ---------- price impact / fillable ----------


def test_price_impact_partial_fill() -> None:
    """100 USDC 完全在 best_ask 一档 (0.42 * 80 = 33.6) → 不够 100, 进下一档 (0.45 * 300)。"""
    snap = _ob()  # asks: (0.42, 80) (0.45, 300) (0.50, 500)
    derived = compute_derived(snap)
    impact_100 = derived.price_impact[0]
    assert impact_100.target_usdc == Decimal("100")
    assert impact_100.filled is True  # 100 USDC 可填满
    # 100 USDC: 第一档吃完 33.6 USDC (80 shares), 第二档吃 66.4 USDC (66.4/0.45 = 147.5 shares)
    # total_shares = 80 + 147.555 = 227.555, avg = 100 / 227.555 = 0.4395
    assert impact_100.avg_price is not None
    assert Decimal("0.43") < impact_100.avg_price < Decimal("0.44")
    assert impact_100.worst_price == Decimal("0.45")


def test_price_impact_unfillable_when_depth_insufficient() -> None:
    """目标 USDC 超过全 ask 总和 → filled=False, 报 fillable_usdc。"""
    # asks 总 USDC = 0.42*80 + 0.45*300 + 0.50*500 = 33.6 + 135 + 250 = 418.6
    snap = _ob()
    derived = compute_derived(snap)
    impact_1000 = derived.price_impact[2]
    assert impact_1000.target_usdc == Decimal("1000")
    assert impact_1000.filled is False
    assert impact_1000.fillable_usdc is not None
    assert impact_1000.fillable_usdc < Decimal("500")  # ~418.6


def test_fillable_within_slippage_caps_at_max_price() -> None:
    """100 bps 滑点 → max_price = 0.42 * 1.01 = 0.4242, 只能吃 best_ask 一档。"""
    snap = _ob()
    derived = compute_derived(snap)
    fill_100bps = derived.fillable_within_slippage[0]
    assert fill_100bps.slippage_bps_max == Decimal("100")
    # max_price = 0.4242, 只有 0.42 一档命中 → 0.42*80 = 33.6
    assert fill_100bps.fillable_usdc == Decimal("33.60")


# ---------- whale ----------


def test_whale_aggregate_threshold() -> None:
    """USDC >= $50 才入 whale 桶。"""
    snap = _ob(
        # bid: 一个 whale (0.40*200=80), 一个非 whale (0.30*100=30)
        bids=[("0.40", "200"), ("0.30", "100")],
        # ask: 都是 whale
        asks=[("0.42", "200"), ("0.45", "300")],
        best_bid_size="200",
        best_ask_size="200",
    )
    derived = compute_derived(snap)
    assert derived.bid_side.whale_count == 1
    assert derived.ask_side.whale_count == 2
    assert derived.bid_side.whale_threshold_usdc == WHALE_THRESHOLD_USDC


# ---------- 15s history aggregates ----------


def test_history_aggregates_compute_volatility_and_rate() -> None:
    """给一组随时间变化的 snapshot, compute_derived 应算出 std / direction_reversals / rate。"""
    base_ts = 1000.0
    samples = []
    for i, (bid, ask) in enumerate([
        ("0.40", "0.42"),
        ("0.41", "0.43"),  # mid 上行
        ("0.40", "0.42"),  # mid 下行 (1 reversal)
        ("0.42", "0.44"),  # mid 上行 (2 reversals)
        ("0.43", "0.45"),  # 持续上行
    ]):
        samples.append((base_ts + i * 1.0, _ob(best_bid=bid, best_ask=ask)))
    derived = compute_derived(samples[-1][1], samples, now_mono=base_ts + 5.0)
    assert derived.sample_count_15s == 5
    # 5 samples / 4 seconds = 1.25 quotes/s
    assert derived.quote_update_rate_per_s is not None
    assert Decimal("1.0") < derived.quote_update_rate_per_s < Decimal("1.5")
    # mid 序列: 0.41, 0.42, 0.41, 0.43, 0.44 — std > 0
    assert derived.mid_volatility_15s is not None
    assert derived.mid_volatility_15s > Decimal("0")
    assert derived.mid_direction_reversals_15s == 2


def test_history_aggregates_empty_window() -> None:
    """无 history (启动早期): 15s 字段全 None / 0."""
    snap = _ob()
    derived = compute_derived(snap, ())
    assert derived.sample_count_15s == 0
    assert derived.mid_volatility_15s is None
    assert derived.spread_volatility_15s is None
    assert derived.quote_update_rate_per_s is None
    assert derived.mid_direction_reversals_15s == 0


# ---------- windows delta ----------


def test_windows_delta_uses_past_snapshot() -> None:
    """构造 10s 前 vs 现在的两份 snapshot, windows[10s] 应算出 best_bid_delta。"""
    past = _ob(best_bid="0.40", best_ask="0.42")
    current = _ob(best_bid="0.43", best_ask="0.45")  # bid 涨 0.03
    samples = [(0.0, past), (10.0, current)]
    derived = compute_derived(current, samples, now_mono=10.0,
                              windows_seconds=DEFAULT_WINDOWS_SECONDS)
    # 找 10s 前 → 找到 past
    w_10 = next(w for w in derived.windows if w.window_s == 10.0)
    assert w_10.available is True
    assert w_10.best_bid_delta == Decimal("0.03")
    assert w_10.best_ask_delta == Decimal("0.03")
    # 2s/3s/5s 前没有比 past 更近的样本——会取最近的 past, available 仍 True
    # (因为 _find_snapshot_at 不要求 ts 严格 < target, 取距 target 最近的)
    # 故所有窗口都 available=True
    for w in derived.windows:
        assert w.available is True


def test_windows_delta_unavailable_when_history_empty() -> None:
    """history 空 → 所有 windows.available=False, delta=0。"""
    snap = _ob()
    derived = compute_derived(snap, ())
    assert all(w.available is False for w in derived.windows)
    for w in derived.windows:
        assert w.bid_total_size_delta == Decimal("0")
        assert w.best_bid_delta is None


# ---------- liquidity_score / tier ----------


def test_liquidity_score_high_when_tight_spread_and_deep_book() -> None:
    """0.5/0.50 spread (=0 bps), 双边 USDC >> $500 → 高分 high。"""
    snap = _ob(
        best_bid="0.50",
        best_ask="0.51",
        best_bid_size="2000",  # 0.50*2000 = $1000
        best_ask_size="2000",  # 0.51*2000 = $1020
        bids=[("0.50", "2000")],
        asks=[("0.51", "2000")],
    )
    derived = compute_derived(snap)
    # 100/0.505 = 198 bps spread (>100), 但 depth 高 + balance 好
    assert derived.liquidity_score > Decimal("0.5")


def test_liquidity_tier_dead_for_empty_book() -> None:
    snap = _ob(best_bid=None, best_ask=None, best_bid_size=None, best_ask_size=None,
               bids=[], asks=[])
    derived = compute_derived(snap)
    assert derived.liquidity_score == Decimal("0")
    assert derived.liquidity_tier == "dead"


# ---------- direction 嵌入 (组合优化 #75) ----------


def test_direction_field_none_by_default() -> None:
    """compute_derived 不传 direction → DerivedMetrics.direction is None."""
    snap = _ob()
    derived = compute_derived(snap)
    assert derived.direction is None


def test_direction_field_passes_through_when_provided() -> None:
    """publisher 传入 DirectionSnapshot → DerivedMetrics.direction 透传该实例."""
    snap = _ob()
    direction = DirectionSnapshot(
        window_seconds=10.0,
        sample_count=42,
        direction_score=Decimal("0.35"),
        price_momentum=Decimal("0.12"),
        flow_imbalance=Decimal("-0.20"),
        direction_label="yes",
        confidence=Decimal("0.85"),
    )
    derived = compute_derived(snap, direction=direction)
    assert derived.direction is direction
    assert derived.direction.direction_label == "yes"
    assert derived.direction.confidence == Decimal("0.85")
