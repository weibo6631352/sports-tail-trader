from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.account_state import (
    _ALLOWANCE_CAP_USDC,
    AccountStateStore,
)


def test_replace_positions_preserves_authoritative_settlement_fields_from_sparse_snapshot() -> None:
    store = AccountStateStore()
    store.replace_positions(
        (
            Position(
                strategy_id="sports_tail",
                condition_id="condition-1",
                token_id="token-1",
                market_slug="match-total",
                shares=Decimal("5000"),
                cost_usdc=Decimal("5"),
                avg_price=Decimal("0.001"),
                initial_value=Decimal("5"),
                current_value=Decimal("0"),
                cash_pnl=Decimal("-5"),
                percent_pnl=Decimal("-100"),
                cur_price=Decimal("0"),
                redeemable=True,
            ),
        )
    )

    store.replace_positions(
        (
            Position(
                strategy_id="sports_tail",
                condition_id="condition-1",
                token_id="token-1",
                shares=Decimal("5000"),
                cost_usdc=Decimal("5"),
            ),
        )
    )

    position = store.snapshot().get_position("condition-1", "token-1")
    assert position is not None
    assert position.market_slug == "match-total"
    assert position.current_value == Decimal("0")
    assert position.cash_pnl == Decimal("-5")
    assert position.percent_pnl == Decimal("-100")
    assert position.cur_price == Decimal("0")
    assert position.redeemable is True


# C7 allowance cap 单测：max-uint256 approval 是 Polymarket 常态——必须 cap 到
# NUMERIC(38, 18) 可表示范围（1e18），否则持久化会 NumericValueOutOfRange overflow。
def test_update_balances_caps_max_uint256_allowance_to_schema_max() -> None:
    store = AccountStateStore()
    # max(uint256) ≈ 1.16e77，远超 1e20 schema 上限
    raw_max_uint256 = Decimal("115792089237316195423570985008687907853269984665640564039457584007913129639935")
    snapshot = store.update_balances(
        balance_usdc=Decimal("100"),
        allowance_usdc=raw_max_uint256,
    )
    assert snapshot.allowance_usdc == _ALLOWANCE_CAP_USDC
    assert snapshot.balance_usdc == Decimal("100")
    # available 仍按 min(balance, allowance) 算 → 100
    assert snapshot.available_usdc == Decimal("100")


def test_update_balances_passes_through_normal_allowance() -> None:
    store = AccountStateStore()
    snapshot = store.update_balances(
        balance_usdc=Decimal("100"),
        allowance_usdc=Decimal("250"),  # 远低于 1e18，原样透传
    )
    assert snapshot.allowance_usdc == Decimal("250")
    assert snapshot.available_usdc == Decimal("100")


def test_update_balances_caps_at_boundary() -> None:
    store = AccountStateStore()
    # 等于 cap 时也透传，> cap 时才 cap
    boundary = _ALLOWANCE_CAP_USDC
    snapshot_boundary = store.update_balances(allowance_usdc=boundary)
    assert snapshot_boundary.allowance_usdc == boundary

    above_cap = _ALLOWANCE_CAP_USDC + Decimal("1")
    snapshot_above = store.update_balances(allowance_usdc=above_cap)
    assert snapshot_above.allowance_usdc == _ALLOWANCE_CAP_USDC
