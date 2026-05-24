"""``OrderbookDerivedPublisher`` 同步刷新行为约束。

关键不变量:
1. refresh() 返回后 store 必含基于本次 snapshot 的 derived (P0 新鲜度对齐).
2. compute 异常被吞掉, 不影响后续 refresh 调用 (P0 推送链路不挂).
3. observability stats 正确累计.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.runtime.orderbook_delta import OrderbookDeltaStore
from polymarket_trader.runtime.orderbook_derived_publisher import OrderbookDerivedPublisher
from polymarket_trader.runtime.orderbook_derived_store import OrderbookDerivedStore
from polymarket_trader.runtime.orderbook_history_buffer import OrderbookHistoryBuffer


def _ob(token_id: str = "tok", best_bid: str = "0.40", best_ask: str = "0.42") -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=token_id,
        best_bid=Decimal(best_bid),
        best_ask=Decimal(best_ask),
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("80"),
        bids=(PriceLevel(price=Decimal(best_bid), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal(best_ask), size=Decimal("80")),),
        received_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
    )


def test_refresh_writes_derived_synchronously() -> None:
    """refresh 返回后 store 立即可读 (P0 新鲜度对齐核心约束)."""
    store = OrderbookDerivedStore()
    buf = OrderbookHistoryBuffer()
    pub = OrderbookDerivedPublisher(store=store, history_buffer=buf)

    snap = _ob("tok-1")
    pub.refresh(snap)

    # 关键: refresh 同步返回后, 立刻能读到 derived (无 await/sleep)
    derived = store.get("tok-1")
    assert derived is not None
    assert derived.token_id == "tok-1"
    assert derived.best_bid == Decimal("0.40")
    assert pub.stats() == {"refreshed_total": 1, "error_total": 0}


def test_refresh_uses_latest_history_window() -> None:
    """publisher 调 history_buffer.samples() 取本次窗口, derived 反映窗口聚合。"""
    store = OrderbookDerivedStore()
    buf = OrderbookHistoryBuffer()
    pub = OrderbookDerivedPublisher(store=store, history_buffer=buf)

    # 先 record 几条到 history (模拟 ws push 历史)
    for i in range(5):
        buf.record(_ob("tok-2", best_bid=f"0.{40 + i}0", best_ask=f"0.{50 + i}0"))

    pub.refresh(_ob("tok-2", best_bid="0.45", best_ask="0.55"))
    derived = store.get("tok-2")
    assert derived is not None
    # 应该有 15s 窗口 sample (含 history record 的几条)
    assert derived.sample_count_15s >= 5


def test_consecutive_refresh_overwrites_with_latest() -> None:
    """连续多次 refresh: store 永远是最后一次 snapshot 对应的 derived (同步顺序执行)。"""
    store = OrderbookDerivedStore()
    buf = OrderbookHistoryBuffer()
    pub = OrderbookDerivedPublisher(store=store, history_buffer=buf)

    for i in range(5):
        pub.refresh(_ob("tok-x", best_bid=f"0.{40 + i}0", best_ask=f"0.{50 + i}0"))

    derived = store.get("tok-x")
    assert derived is not None
    # 最后一次 refresh 的 best_bid=0.44
    assert derived.best_bid == Decimal("0.44")
    assert pub.stats()["refreshed_total"] == 5
    assert pub.stats()["error_total"] == 0


def test_direction_signal_is_embedded_when_delta_store_provided() -> None:
    """注入 delta_store 后, publisher 从 direction_signal() 拼装并嵌入 DerivedMetrics.

    direction_signal 用 ``_utc_now()`` cutoff 取 10s 窗口, 测试 sample 时间戳必须
    在该窗口内才能被纳入 → 用当前时刻向后倒推几秒.
    """
    from datetime import timedelta
    store = OrderbookDerivedStore()
    buf = OrderbookHistoryBuffer()
    delta = OrderbookDeltaStore()
    pub = OrderbookDerivedPublisher(store=store, history_buffer=buf, delta_store=delta)

    now = datetime.now(timezone.utc)
    for i in range(5):
        snap_i = OrderbookSnapshot(
            token_id="tok-dir",
            best_bid=Decimal(f"0.{40+i:02d}"),
            best_ask=Decimal(f"0.{42+i:02d}"),
            best_bid_size=Decimal("100"),
            best_ask_size=Decimal("80"),
            bids=(PriceLevel(price=Decimal(f"0.{40+i:02d}"), size=Decimal("100")),),
            asks=(PriceLevel(price=Decimal(f"0.{42+i:02d}"), size=Decimal("80")),),
            received_at=now - timedelta(seconds=5 - i),  # 5s 前到现在均匀分布
        )
        delta.observe(snap_i)

    pub.refresh(_ob("tok-dir", best_bid="0.44", best_ask="0.46"))
    derived = store.get("tok-dir")
    assert derived is not None
    assert derived.direction is not None
    assert derived.direction.sample_count >= 2
    assert derived.direction.direction_label in {"yes", "no", "neutral"}
    assert derived.direction.window_seconds == 10.0


def test_direction_none_when_delta_store_missing() -> None:
    """不注入 delta_store → DerivedMetrics.direction 永远 None (admin 仍可查独立 endpoint)."""
    store = OrderbookDerivedStore()
    buf = OrderbookHistoryBuffer()
    pub = OrderbookDerivedPublisher(store=store, history_buffer=buf)  # 无 delta_store
    pub.refresh(_ob("tok-no-delta"))
    derived = store.get("tok-no-delta")
    assert derived is not None
    assert derived.direction is None


def test_direction_none_when_sample_insufficient() -> None:
    """delta_store 注入但 sample < 2 → direction_signal 返回 None → DerivedMetrics.direction is None."""
    store = OrderbookDerivedStore()
    buf = OrderbookHistoryBuffer()
    delta = OrderbookDeltaStore()
    pub = OrderbookDerivedPublisher(store=store, history_buffer=buf, delta_store=delta)
    pub.refresh(_ob("tok-thin"))  # delta_store 空, direction_signal 返回 None
    derived = store.get("tok-thin")
    assert derived is not None
    assert derived.direction is None


def test_compute_failure_is_swallowed(monkeypatch) -> None:
    """若 compute_derived 抛异常 → publisher 计 error_total, 不向上抛 (P0 推送链路不挂)。"""
    store = OrderbookDerivedStore()
    buf = OrderbookHistoryBuffer()
    pub = OrderbookDerivedPublisher(store=store, history_buffer=buf)

    import polymarket_trader.runtime.orderbook_derived_publisher as pub_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(pub_mod, "compute_derived", _boom)
    pub.refresh(_ob("tok-err"))  # 不应抛

    assert store.get("tok-err") is None
    assert pub.stats()["error_total"] == 1

    # 恢复正常 compute, 下次 refresh 仍能成功 (publisher 状态干净)
    from polymarket_trader.domain.orderbook_derived import compute_derived as real_compute
    monkeypatch.setattr(pub_mod, "compute_derived", real_compute)
    pub.refresh(_ob("tok-err"))
    assert store.get("tok-err") is not None
    assert pub.stats()["refreshed_total"] == 1
