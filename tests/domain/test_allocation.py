from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.allocation import current_exposure_usdc
from polymarket_trader.domain.position import Position


def test_settled_zero_value_position_does_not_consume_exposure() -> None:
    position = Position(
        condition_id="condition-1",
        token_id="token-1",
        shares=Decimal("5000"),
        cost_usdc=Decimal("5"),
        current_value=Decimal("0"),
        cash_pnl=Decimal("-5"),
        cur_price=Decimal("0"),
        redeemable=True,
    )

    assert current_exposure_usdc(position) == Decimal("0")
