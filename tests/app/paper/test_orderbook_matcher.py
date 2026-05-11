"""沙箱订单簿撮合的单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.app.paper.orderbook_matcher import (
    match_taker_buy,
    match_taker_sell,
)
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel


def _ob(asks: tuple[tuple[str, str], ...] = (), bids: tuple[tuple[str, str], ...] = ()) -> OrderbookSnapshot:
    ask_levels = tuple(PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks)
    bid_levels = tuple(PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids)
    return OrderbookSnapshot(
        token_id="t",
        best_bid=bid_levels[0].price if bid_levels else None,
        best_ask=ask_levels[0].price if ask_levels else None,
        bids=bid_levels,
        asks=ask_levels,
        received_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )


def test_buy_consumes_single_level_fully() -> None:
    ob = _ob(asks=(("0.50", "100"),))
    result = match_taker_buy(ob, amount_usdc=Decimal("10"), limit_price=Decimal("0.55"))
    assert result.filled_shares == Decimal("20")
    assert result.avg_price == Decimal("0.50")
    assert result.gross_notional_usdc == Decimal("10")
    assert result.unfilled_amount_usdc == Decimal("0")
    assert result.is_full_fill


def test_buy_walks_three_levels_with_weighted_avg() -> None:
    ob = _ob(asks=(("0.50", "10"), ("0.55", "10"), ("0.60", "100")))
    # First level: 10 shares × 0.50 = 5 USDC
    # Second level: 10 shares × 0.55 = 5.5 USDC
    # Need 14.5 more from level 3: 14.5 / 0.60 ≈ 24.166666...
    result = match_taker_buy(ob, amount_usdc=Decimal("25"), limit_price=Decimal("0.65"))
    assert result.filled_shares == Decimal("10") + Decimal("10") + Decimal("14.5") / Decimal("0.60")
    assert result.gross_notional_usdc == Decimal("25")
    assert len(result.consumed_levels) == 3
    assert result.is_full_fill


def test_buy_truncates_at_limit_price() -> None:
    ob = _ob(asks=(("0.50", "10"), ("0.60", "100")))
    result = match_taker_buy(ob, amount_usdc=Decimal("100"), limit_price=Decimal("0.55"))
    assert result.filled_shares == Decimal("10")
    assert result.unfilled_amount_usdc == Decimal("100") - Decimal("5")
    assert not result.is_full_fill


def test_buy_returns_no_fill_on_empty_book() -> None:
    ob = _ob(asks=())
    result = match_taker_buy(ob, amount_usdc=Decimal("10"), limit_price=Decimal("0.50"))
    assert result.filled_shares == Decimal("0")
    assert result.avg_price is None
    assert not result.is_filled
    assert result.unfilled_amount_usdc == Decimal("10")


def test_buy_returns_no_fill_when_all_levels_above_limit() -> None:
    ob = _ob(asks=(("0.80", "10"),))
    result = match_taker_buy(ob, amount_usdc=Decimal("5"), limit_price=Decimal("0.50"))
    assert result.filled_shares == Decimal("0")
    assert result.unfilled_amount_usdc == Decimal("5")


def test_buy_market_no_limit_eats_everything() -> None:
    ob = _ob(asks=(("0.99", "5"),))
    result = match_taker_buy(ob, amount_usdc=Decimal("10"), limit_price=None)
    assert result.filled_shares == Decimal("5")
    assert result.gross_notional_usdc == Decimal("4.95")
    assert result.unfilled_amount_usdc == Decimal("5.05")


def test_sell_consumes_top_bid_first() -> None:
    ob = _ob(bids=(("0.45", "20"), ("0.40", "100")))
    result = match_taker_sell(ob, size_shares=Decimal("30"), limit_price=Decimal("0.40"))
    assert result.filled_shares == Decimal("30")
    # 20 × 0.45 + 10 × 0.40 = 9 + 4 = 13
    assert result.gross_notional_usdc == Decimal("13")
    assert result.is_full_fill


def test_sell_truncates_at_lower_limit() -> None:
    ob = _ob(bids=(("0.45", "5"), ("0.40", "100")))
    result = match_taker_sell(ob, size_shares=Decimal("100"), limit_price=Decimal("0.42"))
    assert result.filled_shares == Decimal("5")
    assert result.unfilled_size_shares == Decimal("95")


def test_sell_no_fill_on_empty_book() -> None:
    ob = _ob(bids=())
    result = match_taker_sell(ob, size_shares=Decimal("10"), limit_price=Decimal("0.40"))
    assert not result.is_filled
    assert result.unfilled_size_shares == Decimal("10")


def test_zero_amount_returns_empty() -> None:
    ob = _ob(asks=(("0.50", "10"),))
    result = match_taker_buy(ob, amount_usdc=Decimal("0"), limit_price=None)
    assert not result.is_filled
    assert result.unfilled_amount_usdc == Decimal("0")
