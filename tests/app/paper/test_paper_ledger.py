"""沙箱虚拟账本单元测试。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.app.paper.state import PaperVirtualLedger


def test_buy_fill_deducts_usdc_and_adds_net_shares() -> None:
    ledger = PaperVirtualLedger()
    ledger.fund(Decimal("100"))
    ledger.apply_buy_fill(
        token_id="t1",
        gross_spent_usdc=Decimal("50"),
        gross_filled_shares=Decimal("100"),
        fee_usdc=Decimal("0.5"),
        fee_shares=Decimal("1"),
    )
    assert ledger.available_usdc == Decimal("50")
    assert ledger.position_for("t1") == Decimal("99")
    assert ledger.fees_accrued_usdc == Decimal("0.5")


def test_sell_fill_increases_usdc_net_of_fee_and_decreases_position() -> None:
    ledger = PaperVirtualLedger()
    ledger.positions["t1"] = Decimal("100")
    ledger.apply_sell_fill(
        token_id="t1",
        gross_received_usdc=Decimal("80"),
        gross_filled_shares=Decimal("100"),
        fee_usdc=Decimal("0.4"),
    )
    assert ledger.available_usdc == Decimal("79.6")
    assert ledger.position_for("t1") == Decimal("0")
    assert "t1" not in ledger.positions
    assert ledger.fees_accrued_usdc == Decimal("0.4")


def test_partial_sell_keeps_remaining_position() -> None:
    ledger = PaperVirtualLedger()
    ledger.positions["t1"] = Decimal("100")
    ledger.apply_sell_fill(
        token_id="t1",
        gross_received_usdc=Decimal("20"),
        gross_filled_shares=Decimal("40"),
        fee_usdc=Decimal("0.1"),
    )
    assert ledger.position_for("t1") == Decimal("60")
    assert ledger.available_usdc == Decimal("19.9")


def test_buy_fee_in_shares_does_not_underflow() -> None:
    ledger = PaperVirtualLedger()
    ledger.apply_buy_fill(
        token_id="t1",
        gross_spent_usdc=Decimal("1"),
        gross_filled_shares=Decimal("1"),
        fee_usdc=Decimal("0.01"),
        fee_shares=Decimal("2"),
    )
    assert ledger.position_for("t1") == Decimal("0")


def test_buy_accumulates_cost_basis_for_position_sync() -> None:
    ledger = PaperVirtualLedger()
    ledger.apply_buy_fill(
        token_id="t1",
        gross_spent_usdc=Decimal("10"),
        gross_filled_shares=Decimal("20"),
        fee_usdc=Decimal("0"),
        fee_shares=Decimal("0"),
    )
    ledger.apply_buy_fill(
        token_id="t1",
        gross_spent_usdc=Decimal("5"),
        gross_filled_shares=Decimal("5"),
        fee_usdc=Decimal("0"),
        fee_shares=Decimal("0"),
    )
    assert ledger.position_for("t1") == Decimal("25")
    assert ledger.cost_for("t1") == Decimal("15")


def test_partial_sell_releases_proportional_cost_basis() -> None:
    ledger = PaperVirtualLedger()
    ledger.apply_buy_fill(
        token_id="t1",
        gross_spent_usdc=Decimal("10"),
        gross_filled_shares=Decimal("20"),
        fee_usdc=Decimal("0"),
        fee_shares=Decimal("0"),
    )
    # 卖一半 → 释放一半 cost
    ledger.apply_sell_fill(
        token_id="t1",
        gross_received_usdc=Decimal("8"),
        gross_filled_shares=Decimal("10"),
        fee_usdc=Decimal("0"),
    )
    assert ledger.position_for("t1") == Decimal("10")
    assert ledger.cost_for("t1") == Decimal("5")


def test_full_sell_clears_position_and_cost() -> None:
    ledger = PaperVirtualLedger()
    ledger.apply_buy_fill(
        token_id="t1",
        gross_spent_usdc=Decimal("10"),
        gross_filled_shares=Decimal("20"),
        fee_usdc=Decimal("0"),
        fee_shares=Decimal("0"),
    )
    ledger.apply_sell_fill(
        token_id="t1",
        gross_received_usdc=Decimal("12"),
        gross_filled_shares=Decimal("20"),
        fee_usdc=Decimal("0"),
    )
    assert ledger.position_for("t1") == Decimal("0")
    assert ledger.cost_for("t1") == Decimal("0")
    assert "t1" not in ledger.cost_basis_usdc
