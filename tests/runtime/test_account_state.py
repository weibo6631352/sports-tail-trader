from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.account_state import AccountStateStore


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
