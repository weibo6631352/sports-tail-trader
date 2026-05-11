from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.order import Order, OrderSide, OrderStatus, OrderType


def test_available_usdc_subtracts_open_buy_order_reserve() -> None:
    account = AccountSnapshot(
        balance_usdc=Decimal("5.05236"),
        allowance_usdc=Decimal("100"),
        open_orders=(
            Order(
                strategy_id="sports_tail",
                condition_id="old-condition",
                token_id="old-token",
                side=OrderSide.BUY,
                order_type=OrderType.GTC,
                price=Decimal("0.99"),
                size_shares=Decimal("5.05"),
                remaining_shares=Decimal("5.05"),
                status=OrderStatus.LIVE,
            ),
        ),
    )

    assert account.open_buy_reserved_usdc == Decimal("4.9995")
    assert account.available_usdc == Decimal("0.05286")


def test_available_usdc_ignores_closed_and_sell_orders() -> None:
    account = AccountSnapshot(
        balance_usdc=Decimal("10"),
        allowance_usdc=Decimal("10"),
        open_orders=(
            Order(
                strategy_id="sports_tail",
                condition_id="condition",
                token_id="yes",
                side=OrderSide.BUY,
                order_type=OrderType.GTC,
                price=Decimal("0.90"),
                size_shares=Decimal("5"),
                status=OrderStatus.CANCELLED,
            ),
            Order(
                strategy_id="sports_tail",
                condition_id="condition",
                token_id="yes",
                side=OrderSide.SELL,
                order_type=OrderType.GTC,
                price=Decimal("0.99"),
                size_shares=Decimal("5"),
                status=OrderStatus.LIVE,
            ),
        ),
    )

    assert account.open_buy_reserved_usdc == Decimal("0")
    assert account.available_usdc == Decimal("10")
