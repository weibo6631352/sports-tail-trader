from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.runtime.orderbook_delta import OrderbookDeltaStore


_BASE = datetime(2026, 5, 23, 4, 0, 0, tzinfo=timezone.utc)


def _snap(
    *,
    token_id: str = "token-1",
    bid: str | None = "0.40",
    ask: str | None = "0.42",
    bid_size: str | None = "100",
    ask_size: str | None = "200",
    offset_seconds: float = 0.0,
) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=token_id,
        best_bid=Decimal(bid) if bid is not None else None,
        best_ask=Decimal(ask) if ask is not None else None,
        best_bid_size=Decimal(bid_size) if bid_size is not None else None,
        best_ask_size=Decimal(ask_size) if ask_size is not None else None,
        bids=(),
        asks=(),
        received_at=_BASE + timedelta(seconds=offset_seconds),
    )


def test_observe_dedupes_identical_consecutive_snapshots() -> None:
    """WS 心跳可能重发完全相同的 snapshot,deque 不应被同值塞满。"""

    store = OrderbookDeltaStore()
    store.observe(_snap(offset_seconds=0))
    store.observe(_snap(offset_seconds=1))  # 相同值
    store.observe(_snap(offset_seconds=2))
    assert len(store.samples("token-1")) == 1


def test_direction_signal_returns_none_with_insufficient_samples() -> None:
    store = OrderbookDeltaStore()
    store.observe(_snap(offset_seconds=0))
    signal = store.direction_signal("token-1", window_seconds=10, now=_BASE + timedelta(seconds=5))
    assert signal is None


def test_direction_signal_yes_when_mid_moves_up() -> None:
    """bid 0.40→0.45 + ask 0.42→0.47 → mid_delta=+0.05,显著 YES 方向。"""

    store = OrderbookDeltaStore()
    store.observe(_snap(bid="0.40", ask="0.42", offset_seconds=0))
    store.observe(_snap(bid="0.45", ask="0.47", offset_seconds=5))
    signal = store.direction_signal("token-1", window_seconds=10, now=_BASE + timedelta(seconds=6))
    assert signal is not None
    assert signal.direction_label == "yes"
    assert signal.direction_score > Decimal("0.5")
    assert signal.mid_price_delta == Decimal("0.05")
    assert signal.bid_price_delta == Decimal("0.05")


def test_direction_signal_no_when_mid_moves_down() -> None:
    """bid 0.40→0.35 + ask 0.42→0.37 → mid_delta=-0.05,NO 方向。"""

    store = OrderbookDeltaStore()
    store.observe(_snap(bid="0.40", ask="0.42", offset_seconds=0))
    store.observe(_snap(bid="0.35", ask="0.37", offset_seconds=5))
    signal = store.direction_signal("token-1", window_seconds=10, now=_BASE + timedelta(seconds=6))
    assert signal is not None
    assert signal.direction_label == "no"
    assert signal.direction_score < Decimal("-0.5")


def test_direction_signal_neutral_when_mid_barely_moves() -> None:
    """微小价位移(< 0.001) → neutral。"""

    store = OrderbookDeltaStore()
    store.observe(_snap(bid="0.40", ask="0.42", offset_seconds=0))
    store.observe(_snap(bid="0.401", ask="0.421", offset_seconds=5))
    signal = store.direction_signal("token-1", window_seconds=10, now=_BASE + timedelta(seconds=6))
    assert signal is not None
    assert signal.direction_label == "neutral"


def test_direction_signal_window_filters_old_samples() -> None:
    """超出 window 的旧 sample 不参与计算。"""

    store = OrderbookDeltaStore()
    store.observe(_snap(bid="0.20", ask="0.22", offset_seconds=0))
    store.observe(_snap(bid="0.50", ask="0.52", offset_seconds=100))  # 100s 后
    # window=5s,now=t=105 → 只有 t=100 那一条 in-window,sample_count=1 → None
    signal = store.direction_signal("token-1", window_seconds=5, now=_BASE + timedelta(seconds=105))
    assert signal is None


def test_direction_signal_ask_size_consumption_tracked() -> None:
    """ask size 被消耗(200→50) 在 size_delta 中可见,即使 price 不变。"""

    store = OrderbookDeltaStore()
    store.observe(_snap(bid="0.40", ask="0.42", ask_size="200", offset_seconds=0))
    store.observe(_snap(bid="0.40", ask="0.42", ask_size="50", offset_seconds=5))
    signal = store.direction_signal("token-1", window_seconds=10, now=_BASE + timedelta(seconds=6))
    assert signal is not None
    assert signal.ask_size_delta == Decimal("-150")  # 被吃 150


def test_confidence_increases_with_sample_count() -> None:
    store = OrderbookDeltaStore()
    for i in range(2):
        store.observe(_snap(bid=f"0.{40 + i}", ask=f"0.{42 + i}", offset_seconds=i))
    low = store.direction_signal("token-1", window_seconds=30, now=_BASE + timedelta(seconds=5))
    for i in range(2, 12):
        store.observe(_snap(bid=f"0.{40 + i % 10}", ask=f"0.{42 + i % 10}", offset_seconds=i))
    high = store.direction_signal("token-1", window_seconds=30, now=_BASE + timedelta(seconds=15))
    assert low is not None and high is not None
    assert high.confidence >= low.confidence


def test_prune_removes_samples_older_than_max_window() -> None:
    store = OrderbookDeltaStore(max_window_seconds=10.0)
    store.observe(_snap(offset_seconds=0))
    store.observe(_snap(bid="0.50", offset_seconds=5))
    store.observe(_snap(bid="0.60", offset_seconds=100))
    removed = store.prune(now=_BASE + timedelta(seconds=105))
    assert removed == 2
    assert len(store.samples("token-1")) == 1


def test_per_token_isolation() -> None:
    store = OrderbookDeltaStore()
    store.observe(_snap(token_id="a", bid="0.40", offset_seconds=0))
    store.observe(_snap(token_id="b", bid="0.80", offset_seconds=0))
    store.observe(_snap(token_id="a", bid="0.45", offset_seconds=5))
    assert len(store.samples("a")) == 2
    assert len(store.samples("b")) == 1
    sa = store.direction_signal("a", window_seconds=10, now=_BASE + timedelta(seconds=6))
    assert sa is not None and sa.direction_label == "yes"
