from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.order import (
    BuyOrderIntent,
    Order,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderStatus,
    OrderType,
)
from polymarket_trader.infra.db.models import OrderModel


def test_order_result_persistence_key_prefers_exchange_order_id_over_intent_key() -> None:
    intent = BuyOrderIntent(
        trace_id="trace-1",
        condition_id="condition-1",
        token_id="token-1",
        price=Decimal("0.71"),
        amount_usdc=Decimal("5"),
        idempotency_key="local-idempotency-key",
    )
    result = OrderResult(
        trace_id="trace-1",
        condition_id="condition-1",
        token_id="token-1",
        status=OrderResultStatus.FULL_FILL,
        intent=intent,
        order_id="exchange-order-id",
        trade_id="trade-1",
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        price=Decimal("0.71"),
        requested_amount_usdc=Decimal("5"),
        matched_shares=Decimal("7.04"),
        spent_usdc=Decimal("5"),
        notional_usdc=Decimal("5"),
    )

    model = OrderModel.from_domain(result)

    assert model.order_key == "exchange-order-id"


def test_order_persistence_key_prefers_exchange_order_id_over_local_key() -> None:
    order = Order(
        trace_id="trace-1",
        condition_id="condition-1",
        token_id="token-1",
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        price=Decimal("0.99"),
        amount_usdc=Decimal("5"),
        order_id="exchange-order-id",
        status=OrderStatus.MATCHED,
        idempotency_key="local-idempotency-key",
    )

    model = OrderModel.from_domain(order)

    assert model.order_key == "exchange-order-id"


def test_long_local_order_keys_are_shortened_for_database_columns() -> None:
    long_key = "order:submit:" + "x" * 320
    order = Order(
        trace_id="trace-1",
        condition_id="condition-1",
        token_id="token-1",
        side=OrderSide.SELL,
        order_type=OrderType.GTC,
        price=Decimal("0.99"),
        size_shares=Decimal("5"),
        status=OrderStatus.SUBMITTED,
        idempotency_key=long_key,
    )

    model = OrderModel.from_domain(order)

    assert len(model.order_key) <= 255
    assert len(model.idempotency_key or "") <= 255
    assert model.order_key == model.idempotency_key
    assert model.raw_payload["idempotency_key"] == long_key
