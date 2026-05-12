"""共享 edge_gates 单元测试：验证抽取行为不变 + family-agnostic 字符串拒绝原因。"""

from __future__ import annotations

from decimal import Decimal

from strategies.current._shared.edge_gates import (
    REASON_INSUFFICIENT_EDGE,
    REASON_LIQUIDITY_BELOW_MIN,
    REASON_MISSING_BEST_ASK,
    REASON_PRICE_ABOVE_FAIR,
    check_entry_gates,
    entry_price_cap,
)


def test_entry_price_cap_subtracts_edge() -> None:
    cap = entry_price_cap(
        Decimal("0.40"),
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
    )
    assert cap == Decimal("0.380")


def test_entry_price_cap_clamps_to_max_entry_price() -> None:
    cap = entry_price_cap(
        Decimal("0.95"),
        min_edge_bps=100,
        max_entry_price=Decimal("0.85"),
    )
    assert cap == Decimal("0.85")


def test_entry_price_cap_clamps_to_floor() -> None:
    cap = entry_price_cap(
        Decimal("0.005"),
        min_edge_bps=0,
        max_entry_price=Decimal("0.50"),
    )
    assert cap == Decimal("0.01")


def test_gate_rejects_missing_best_ask() -> None:
    res = check_entry_gates(
        fair_value=Decimal("0.40"),
        best_ask=None,
        buyable_liquidity_usdc=Decimal("500"),
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("100"),
    )
    assert res.passed is False
    assert res.reason == REASON_MISSING_BEST_ASK


def test_gate_rejects_thin_liquidity() -> None:
    res = check_entry_gates(
        fair_value=Decimal("0.40"),
        best_ask=Decimal("0.30"),
        buyable_liquidity_usdc=Decimal("10"),
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("100"),
    )
    assert res.passed is False
    assert res.reason == REASON_LIQUIDITY_BELOW_MIN
    assert res.metadata["buyable_liquidity_usdc"] == "10"


def test_gate_rejects_insufficient_edge() -> None:
    res = check_entry_gates(
        fair_value=Decimal("0.40"),
        best_ask=Decimal("0.39"),
        buyable_liquidity_usdc=Decimal("500"),
        min_edge_bps=500,  # cap = 0.380, ask 0.39 > cap
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("100"),
    )
    assert res.passed is False
    assert res.reason == REASON_INSUFFICIENT_EDGE
    assert res.entry_price_cap == Decimal("0.380")


def test_gate_rejects_price_at_or_above_fair() -> None:
    # 设 edge=0 让 cap=fair；ask == fair 触发 PRICE_ABOVE_FAIR。
    res = check_entry_gates(
        fair_value=Decimal("0.40"),
        best_ask=Decimal("0.40"),
        buyable_liquidity_usdc=Decimal("500"),
        min_edge_bps=0,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("100"),
    )
    assert res.passed is False
    assert res.reason == REASON_PRICE_ABOVE_FAIR


def test_gate_passes_when_all_conditions_met() -> None:
    res = check_entry_gates(
        fair_value=Decimal("0.40"),
        best_ask=Decimal("0.30"),
        buyable_liquidity_usdc=Decimal("500"),
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("100"),
    )
    assert res.passed is True
    assert res.reason is None
    assert res.entry_price_cap == Decimal("0.380")
    assert res.metadata["best_ask"] == "0.30"
