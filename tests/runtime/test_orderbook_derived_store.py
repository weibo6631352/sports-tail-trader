"""``OrderbookDerivedStore`` LRU + prune callback 行为约束。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.orderbook_derived import compute_derived
from polymarket_trader.runtime.orderbook_derived_store import OrderbookDerivedStore


def _derived(token_id: str):
    snap = OrderbookSnapshot(
        token_id=token_id,
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.42"),
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("80"),
        bids=(PriceLevel(price=Decimal("0.40"), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal("0.42"), size=Decimal("80")),),
        received_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
    )
    return compute_derived(snap)


def test_set_get_basic() -> None:
    store = OrderbookDerivedStore()
    d = _derived("tok-1")
    store.set(d)
    fetched = store.get("tok-1")
    assert fetched is d
    assert store.tracked_token_count() == 1


def test_get_missing_returns_none() -> None:
    store = OrderbookDerivedStore()
    assert store.get("nonexistent") is None


def test_lru_eviction_at_cap() -> None:
    """超 cap 时踢最久未访问 (按 set 顺序的最早一个)。"""
    store = OrderbookDerivedStore(max_tokens=3)
    for i in range(5):
        store.set(_derived(f"tok-{i}"))
    assert store.tracked_token_count() == 3
    # tok-0 / tok-1 应被踢
    assert store.get("tok-0") is None
    assert store.get("tok-1") is None
    assert store.get("tok-2") is not None
    assert store.get("tok-3") is not None
    assert store.get("tok-4") is not None


def test_set_updates_lru_order() -> None:
    """re-set 同一个 token 更新 LRU 位置 (移到末尾, 优先级最高)。"""
    store = OrderbookDerivedStore(max_tokens=3)
    store.set(_derived("a"))
    store.set(_derived("b"))
    store.set(_derived("c"))
    # 触摸 a (re-set) → a 移到末尾, 下次 evict 时 b 先被踢
    store.set(_derived("a"))
    # 加 d → 触发 evict, b 该被踢 (最久未 set 的)
    store.set(_derived("d"))
    assert store.get("a") is not None
    assert store.get("b") is None
    assert store.get("c") is not None
    assert store.get("d") is not None


def test_evict_market_clears_listed_tokens() -> None:
    """prune callback: 按 token_ids 清理。"""
    store = OrderbookDerivedStore()
    for tid in ("tok-A", "tok-B", "tok-C"):
        store.set(_derived(tid))
    store.evict_market("cid-1", ("tok-A", "tok-B"))
    assert store.get("tok-A") is None
    assert store.get("tok-B") is None
    assert store.get("tok-C") is not None  # 未列出的不动


def test_memory_footprint_estimate() -> None:
    store = OrderbookDerivedStore(max_tokens=50)
    store.set(_derived("x"))
    store.set(_derived("y"))
    fp = store.memory_footprint_estimate()
    assert fp == {"tracked_tokens": 2, "max_tokens": 50}
