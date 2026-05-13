from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.allocation import current_exposure_usdc
from polymarket_trader.domain.order import OrderRecord, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.position import Position


def _position(
    *,
    shares: str = "10",
    cost_usdc: str = "5",
    open_sell_shares: str = "0",
    redeemable: bool | None = None,
    current_value: str | None = None,
    cur_price: str | None = None,
    cash_pnl: str | None = None,
) -> Position:
    return Position(
        strategy_id="sports_tail",
        condition_id="cond-1",
        token_id="tok-1",
        shares=Decimal(shares),
        cost_usdc=Decimal(cost_usdc),
        open_sell_shares=Decimal(open_sell_shares),
        redeemable=redeemable,
        current_value=Decimal(current_value) if current_value is not None else None,
        cur_price=Decimal(cur_price) if cur_price is not None else None,
        cash_pnl=Decimal(cash_pnl) if cash_pnl is not None else None,
    )


def _buy_order(
    *,
    order_type: OrderType = OrderType.GTC,
    amount_usdc: str | None = None,
    notional_usdc: str | None = None,
    price: str | None = "0.5",
    size_shares: str | None = "10",
) -> OrderRecord:
    return OrderRecord(
        strategy_id="sports_tail",
        condition_id="cond-1",
        token_id="tok-1",
        side=OrderSide.BUY,
        order_type=order_type,
        price=Decimal(price) if price is not None else Decimal("0"),
        amount_usdc=Decimal(amount_usdc) if amount_usdc is not None else None,
        notional_usdc=Decimal(notional_usdc) if notional_usdc is not None else None,
        size_shares=Decimal(size_shares) if size_shares is not None else None,
        status=OrderStatus.LIVE,
    )


def test_settled_zero_value_position_does_not_consume_exposure() -> None:
    position = _position(
        shares="5000",
        cost_usdc="5",
        current_value="0",
        cash_pnl="-5",
        cur_price="0",
        redeemable=True,
    )

    assert current_exposure_usdc(position) == Decimal("0")


def test_none_position_returns_zero() -> None:
    assert current_exposure_usdc(None) == Decimal("0")


def test_active_position_uses_cost_usdc() -> None:
    position = _position(cost_usdc="8")
    assert current_exposure_usdc(position) == Decimal("8")


def test_open_sell_shares_add_proportional_exposure() -> None:
    # 50% of shares listed as open sell → adds 50% of cost_usdc
    position = _position(shares="100", cost_usdc="10", open_sell_shares="50")
    assert current_exposure_usdc(position) == Decimal("10") + Decimal("5")


def test_open_sell_shares_capped_at_total_shares() -> None:
    # open_sell_shares > shares → cap at shares (100%)
    position = _position(shares="10", cost_usdc="8", open_sell_shares="20")
    assert current_exposure_usdc(position) == Decimal("8") + Decimal("8")


def test_fak_buy_order_not_counted() -> None:
    order = _buy_order(order_type=OrderType.FAK, amount_usdc="5")
    assert current_exposure_usdc(None, [order]) == Decimal("0")


def test_sell_order_not_counted() -> None:
    sell = OrderRecord(
        strategy_id="sports_tail",
        condition_id="cond-1",
        token_id="tok-1",
        side=OrderSide.SELL,
        order_type=OrderType.GTC,
        price=Decimal("0.6"),
        size_shares=Decimal("10"),
        status=OrderStatus.LIVE,
    )
    assert current_exposure_usdc(None, [sell]) == Decimal("0")


def test_buy_order_amount_usdc_counted() -> None:
    order = _buy_order(amount_usdc="7", notional_usdc=None, price=None, size_shares=None)
    assert current_exposure_usdc(None, [order]) == Decimal("7")


def test_buy_order_notional_usdc_counted_when_no_amount() -> None:
    order = _buy_order(notional_usdc="6", amount_usdc=None, price=None, size_shares=None)
    assert current_exposure_usdc(None, [order]) == Decimal("6")


def test_buy_order_price_times_shares_counted_as_fallback() -> None:
    order = _buy_order(price="0.4", size_shares="20", amount_usdc=None, notional_usdc=None)
    assert current_exposure_usdc(None, [order]) == Decimal("8")


def test_position_and_orders_sum() -> None:
    position = _position(cost_usdc="5")
    order = _buy_order(amount_usdc="3")
    assert current_exposure_usdc(position, [order]) == Decimal("8")
