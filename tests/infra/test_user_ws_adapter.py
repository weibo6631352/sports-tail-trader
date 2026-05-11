from __future__ import annotations

from decimal import Decimal

from polymarket_trader.infra.polymarket import normalize_fill_payload
from polymarket_trader.infra.polymarket.user_ws_adapter import iter_fill_snapshots


def test_maker_trade_uses_owned_maker_leg_instead_of_top_level_taker_trade() -> None:
    fills = tuple(
        iter_fill_snapshots(
            {
                "event_type": "trade",
                "type": "TRADE",
                "id": "trade-maker",
                "taker_order_id": "large-taker-order",
                "market": "condition-1",
                "asset_id": "taker-token",
                "side": "BUY",
                "size": "4549.39",
                "price": "0.97",
                "status": "MATCHED",
                "matchtime": "1777381019",
                "last_update": "1777381019",
                "outcome": "Mirra Andreeva",
                "trader_side": "MAKER",
                "maker_orders": [
                    {
                        "order_id": "our-exit-order",
                        "owner": "our-api-key",
                        "matched_amount": "5.15",
                        "price": "0.99",
                        "asset_id": "maker-token",
                        "outcome": "Mirra Andreeva",
                        "side": "SELL",
                    }
                ],
            }
        , strategy_id="sports_tail")
    )

    assert len(fills) == 1
    assert fills[0].order_id == "our-exit-order"
    assert fills[0].trade_id == "trade-maker"
    assert fills[0].condition_id == "condition-1"
    assert fills[0].token_id == "maker-token"
    assert fills[0].side == "sell"
    assert fills[0].size == Decimal("5.15")
    assert fills[0].price == Decimal("0.99")
    assert fills[0].notional_usdc == Decimal("5.0985")


def test_taker_trade_keeps_top_level_user_leg() -> None:
    fills = tuple(
        iter_fill_snapshots(
            {
                "event_type": "trade",
                "type": "TRADE",
                "id": "trade-taker",
                "taker_order_id": "our-taker-order",
                "market": "condition-1",
                "asset_id": "taker-token",
                "side": "BUY",
                "size": "5.154633",
                "price": "0.97",
                "status": "MATCHED",
                "matchtime": "1777380900",
                "trader_side": "TAKER",
                "maker_orders": [
                    {
                        "order_id": "other-maker-order",
                        "matched_amount": "5.154633",
                        "price": "0.97",
                        "asset_id": "taker-token",
                        "side": "SELL",
                    }
                ],
            }
        , strategy_id="sports_tail")
    )

    assert len(fills) == 1
    assert fills[0].order_id == "our-taker-order"
    assert fills[0].token_id == "taker-token"
    assert fills[0].side == "buy"
    assert fills[0].size == Decimal("5.154633")


def test_clob_fill_payload_uses_owned_maker_leg_for_historical_trade() -> None:
    fill = normalize_fill_payload(
        {
            "id": "trade-maker",
            "taker_order_id": "large-taker-order",
            "market": "condition-1",
            "asset_id": "taker-token",
            "side": "BUY",
            "size": "4549.39",
            "price": "0.97",
            "status": "MATCHED",
            "matchTime": "1777381019",
            "trader_side": "MAKER",
            "maker_orders": [
                {
                    "order_id": "other-maker-order",
                    "maker_address": "0x1111111111111111111111111111111111111111",
                    "matched_amount": "2985",
                    "price": "0.03",
                    "asset_id": "other-token",
                    "side": "BUY",
                },
                {
                    "order_id": "our-exit-order",
                    "maker_address": "0x78dE3c8264C546Fffed8D9A1396cddEf7c8686BE",
                    "matched_amount": "5.15",
                    "price": "0.99",
                    "asset_id": "maker-token",
                    "side": "SELL",
                }
            ],
        },
        user_address="0x78de3c8264c546fffed8d9a1396cddef7c8686be",
    ).to_fill(strategy_id="sports_tail")

    assert fill.order_id == "our-exit-order"
    assert fill.trade_id == "trade-maker"
    assert fill.condition_id == "condition-1"
    assert fill.token_id == "maker-token"
    assert fill.side == "SELL"
    assert fill.size == Decimal("5.15")
    assert fill.price == Decimal("0.99")
    assert fill.notional_usdc == Decimal("5.0985")
