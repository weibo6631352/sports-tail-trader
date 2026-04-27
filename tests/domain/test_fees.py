from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.fees import build_taker_fee_preview, calculate_trade_fee
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from tests.helpers.markets import build_binary_market


def _market(
    *,
    fees_enabled: bool | None = True,
    taker_base_fee_bps: int | None = 30,
    fee_rate_bps: int | None = 30,
) -> Market:
    return build_binary_market(
        condition_id="condition-1",
        market_slug="sample-market",
        no_token_id="no-token-1",
        yes_token_id="yes-token-1",
        fees_enabled=fees_enabled,
        taker_base_fee_bps=taker_base_fee_bps,
        fee_rate_bps=fee_rate_bps,
    )


def _orderbook(*, bid: str = "0.48", ask: str = "0.52") -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id="no-token-1",
        condition_id="condition-1",
        market_slug="sample-market",
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("100"),
        bids=(PriceLevel(price=Decimal(bid), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal(ask), size=Decimal("100")),),
        received_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )


def test_calculate_trade_fee_matches_polymarket_formula_for_taker_buy() -> None:
    quote = calculate_trade_fee(
        price=Decimal("0.50"),
        size_shares=Decimal("100"),
        side="buy",
        fee_rate_bps=30,
    )

    assert quote.fee_rate_bps == 30
    assert quote.fee_usdc == Decimal("0.75000")
    assert quote.fee_shares == Decimal("1.50000")
    assert quote.charged_in == "shares"
    assert quote.side == "buy"
    assert quote.liquidity_role == "taker"


def test_calculate_trade_fee_returns_zero_for_maker_orders() -> None:
    quote = calculate_trade_fee(
        price=Decimal("0.50"),
        size_shares=Decimal("100"),
        side="sell",
        fee_rate_bps=30,
        liquidity_role="maker",
    )

    assert quote.fee_rate_bps == 0
    assert quote.fee_usdc == Decimal("0.00000")
    assert quote.fee_shares is None
    assert quote.charged_in == "usdc"


def test_build_taker_fee_preview_uses_market_fee_rate_when_available() -> None:
    preview = build_taker_fee_preview(
        market=_market(taker_base_fee_bps=100, fee_rate_bps=125),
        orderbook=_orderbook(bid="0.55", ask="0.59"),
    )

    assert preview is not None
    assert preview.fee_rate_bps == 125
    assert preview.buy is not None
    assert preview.buy.fee_usdc == Decimal("3.02375")
    assert preview.buy.fee_shares == Decimal("5.12500")
    assert preview.buy.price_source == "best_ask"
    assert preview.sell is not None
    assert preview.sell.fee_usdc == Decimal("3.09375")
    assert preview.sell.fee_shares is None
    assert preview.sell.price_source == "best_bid"


def test_build_taker_fee_preview_falls_back_to_market_taker_base_fee() -> None:
    preview = build_taker_fee_preview(
        market=_market(taker_base_fee_bps=30, fee_rate_bps=None),
        orderbook=_orderbook(),
    )

    assert preview is not None
    assert preview.fee_rate_bps == 30
    assert preview.buy is not None
    assert preview.buy.fee_usdc == Decimal("0.74880")
    assert preview.sell is not None
    assert preview.sell.fee_usdc == Decimal("0.74880")


def test_build_taker_fee_preview_returns_zero_quotes_when_fees_disabled() -> None:
    preview = build_taker_fee_preview(
        market=_market(fees_enabled=False, taker_base_fee_bps=None, fee_rate_bps=None),
        orderbook=_orderbook(),
    )

    assert preview is not None
    assert preview.fee_rate_bps == 0
    assert preview.buy is not None
    assert preview.buy.fee_usdc == Decimal("0.00000")
    assert preview.buy.fee_shares == Decimal("0.00000")
    assert preview.sell is not None
    assert preview.sell.fee_usdc == Decimal("0.00000")
