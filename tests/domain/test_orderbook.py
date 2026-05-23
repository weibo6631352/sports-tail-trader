from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from polymarket_trader.domain.orderbook import (
    AskLiquidityState,
    BidLiquidityState,
    OrderbookSnapshot,
    PriceLevel,
)


def _snap(
    *,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    best_bid: str | None = None,
    best_ask: str | None = None,
    best_bid_size: str | None = None,
    best_ask_size: str | None = None,
) -> OrderbookSnapshot:
    bid_levels = tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in (bids or []))
    ask_levels = tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in (asks or []))
    # 测试入口允许显式覆盖 best 价位+size；不指定时从 levels 推断。
    if best_bid is None and bid_levels:
        best_bid_dec = max(level.price for level in bid_levels)
    else:
        best_bid_dec = Decimal(best_bid) if best_bid is not None else None
    if best_ask is None and ask_levels:
        best_ask_dec = min(level.price for level in ask_levels)
    else:
        best_ask_dec = Decimal(best_ask) if best_ask is not None else None
    if best_bid_size is None and bid_levels and best_bid_dec is not None:
        best_bid_size_dec = next(
            (level.size for level in bid_levels if level.price == best_bid_dec), None
        )
    else:
        best_bid_size_dec = Decimal(best_bid_size) if best_bid_size is not None else None
    if best_ask_size is None and ask_levels and best_ask_dec is not None:
        best_ask_size_dec = next(
            (level.size for level in ask_levels if level.price == best_ask_dec), None
        )
    else:
        best_ask_size_dec = Decimal(best_ask_size) if best_ask_size is not None else None
    return OrderbookSnapshot(
        token_id="tok-1",
        best_bid=best_bid_dec,
        best_ask=best_ask_dec,
        bids=bid_levels,
        asks=ask_levels,
        received_at=datetime.now(timezone.utc),
        best_bid_size=best_bid_size_dec,
        best_ask_size=best_ask_size_dec,
    )


class TestBidLiquidityState:
    def test_no_bid_when_bids_empty(self) -> None:
        s = _snap(asks=[("0.50", "100")])
        assert s.bid_liquidity_state == BidLiquidityState.NO_BID
        assert s.sell_actionable is False

    def test_ceiling_only_when_best_bid_is_099(self) -> None:
        # 只有 MM 在 0.99 接 SELL 的天花板单——不是真买盘
        s = _snap(bids=[("0.99", "500")], asks=[("0.999", "10")])
        assert s.bid_liquidity_state == BidLiquidityState.CEILING_ONLY
        assert s.sell_actionable is False

    def test_dust_bid_when_best_size_below_5usdc(self) -> None:
        # best_bid=0.50, size=3 → 1.5 USDC < 5
        s = _snap(bids=[("0.50", "3")], asks=[("0.55", "50")])
        assert s.bid_liquidity_state == BidLiquidityState.DUST_BID
        assert s.sell_actionable is False

    def test_ok_when_real_bid_with_size(self) -> None:
        # 0.50 × 100 = 50 USDC > 5
        s = _snap(bids=[("0.50", "100")], asks=[("0.55", "50")])
        assert s.bid_liquidity_state == BidLiquidityState.OK
        assert s.sell_actionable is True


class TestAskLiquidityState:
    def test_no_ask_when_asks_empty(self) -> None:
        s = _snap(bids=[("0.50", "100")])
        assert s.ask_liquidity_state == AskLiquidityState.NO_ASK
        assert s.buy_actionable is False

    def test_floor_only_when_best_ask_is_001(self) -> None:
        # 0.01 ask = MM 兜底 BUY 单
        s = _snap(bids=[("0.005", "10")], asks=[("0.01", "5000")])
        assert s.ask_liquidity_state == AskLiquidityState.FLOOR_ONLY
        assert s.buy_actionable is False

    def test_dust_ask_when_best_size_below_5usdc(self) -> None:
        s = _snap(bids=[("0.50", "100")], asks=[("0.55", "5")])
        # 0.55 × 5 = 2.75 < 5
        assert s.ask_liquidity_state == AskLiquidityState.DUST_ASK
        assert s.buy_actionable is False

    def test_ok_when_real_ask_with_size(self) -> None:
        s = _snap(bids=[("0.50", "100")], asks=[("0.55", "50")])
        assert s.ask_liquidity_state == AskLiquidityState.OK
        assert s.buy_actionable is True


class TestMicroprice:
    def test_none_when_no_bid(self) -> None:
        s = _snap(asks=[("0.55", "50")])
        assert s.microprice is None

    def test_none_when_ceiling_only(self) -> None:
        s = _snap(bids=[("0.99", "500")], asks=[("0.995", "10")])
        assert s.microprice is None

    def test_none_when_no_ask(self) -> None:
        s = _snap(bids=[("0.50", "100")])
        assert s.microprice is None

    def test_none_when_floor_only(self) -> None:
        s = _snap(bids=[("0.005", "10")], asks=[("0.01", "5000")])
        assert s.microprice is None

    def test_microprice_cross_weighted(self) -> None:
        # bid=0.40 (size=1000), ask=0.42 (size=50)
        # microprice = (0.42 * 1000 + 0.40 * 50) / 1050 = (420 + 20) / 1050 ≈ 0.4190
        s = _snap(bids=[("0.40", "1000")], asks=[("0.42", "50")])
        mp = s.microprice
        assert mp is not None
        # 应该明显偏向 ask（薄的一侧）
        assert mp > Decimal("0.41")  # > mid
        assert mp < Decimal("0.42")
        # 精确值
        expected = (Decimal("0.42") * Decimal("1000") + Decimal("0.40") * Decimal("50")) / Decimal("1050")
        assert mp == expected

    def test_microprice_falls_back_to_mid_when_sizes_missing(self) -> None:
        snap = OrderbookSnapshot(
            token_id="t",
            best_bid=Decimal("0.40"),
            best_ask=Decimal("0.42"),
            bids=(PriceLevel(Decimal("0.40"), Decimal("100")),),
            asks=(PriceLevel(Decimal("0.42"), Decimal("100")),),
            received_at=datetime.now(timezone.utc),
            best_bid_size=None,
            best_ask_size=None,
        )
        assert snap.microprice == Decimal("0.41")

    def test_microprice_symmetric_when_equal_sizes(self) -> None:
        s = _snap(bids=[("0.40", "100")], asks=[("0.42", "100")])
        assert s.microprice == Decimal("0.41")
