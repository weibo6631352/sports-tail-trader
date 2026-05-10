from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.extension_api.toolkit import (
    depth_at_price,
    depth_weighted_price,
    midpoint,
    spread_bps,
)


def _orderbook(
    *,
    bids: tuple[tuple[str, str], ...] = (),
    asks: tuple[tuple[str, str], ...] = (),
    best_bid: str | None = None,
    best_ask: str | None = None,
) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id="tok-1",
        best_bid=Decimal(best_bid) if best_bid is not None else None,
        best_ask=Decimal(best_ask) if best_ask is not None else None,
        bids=tuple(PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids),
        asks=tuple(PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks),
        received_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )


def test_midpoint_returns_average() -> None:
    book = _orderbook(best_bid="0.40", best_ask="0.50")
    assert midpoint(book) == Decimal("0.45")


def test_midpoint_returns_none_when_side_missing() -> None:
    assert midpoint(None) is None
    assert midpoint(_orderbook(best_bid="0.40")) is None
    assert midpoint(_orderbook(best_ask="0.50")) is None


def test_spread_bps_in_basis_points() -> None:
    book = _orderbook(best_bid="0.40", best_ask="0.50")
    # midpoint=0.45, spread=0.10 → 0.10/0.45 * 10000 ≈ 2222.22
    bps = spread_bps(book)
    assert bps is not None and abs(bps - Decimal("2222.222222222222222222222222")) < Decimal("0.01")


def test_depth_at_price_ask_side() -> None:
    book = _orderbook(asks=(("0.50", "10"), ("0.55", "20"), ("0.60", "30")))
    assert depth_at_price(book, side="ask", price=Decimal("0.55")) == Decimal("30")  # 10 + 20
    assert depth_at_price(book, side="ask", price=Decimal("0.60")) == Decimal("60")  # all


def test_depth_at_price_bid_side() -> None:
    book = _orderbook(bids=(("0.50", "10"), ("0.45", "20")))
    assert depth_at_price(book, side="bid", price=Decimal("0.50")) == Decimal("10")
    assert depth_at_price(book, side="bid", price=Decimal("0.40")) == Decimal("30")


def test_depth_weighted_price_eats_through_levels() -> None:
    book = _orderbook(asks=(("0.50", "5"), ("0.60", "5")))
    # 吃 8 shares: 5 @ 0.50 + 3 @ 0.60 = 2.5 + 1.8 = 4.3 / 8 = 0.5375
    avg = depth_weighted_price(book, side="ask", target_size=Decimal("8"))
    assert avg == Decimal("0.5375")


def test_depth_weighted_price_returns_none_when_insufficient() -> None:
    book = _orderbook(asks=(("0.50", "5"),))
    assert depth_weighted_price(book, side="ask", target_size=Decimal("100")) is None
