"""沙箱 fill engine 单元测试：四种 fill 状态 + fee 透传 + GTC 行为。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.app.paper.fill_engine import simulate_fill
from polymarket_trader.app.paper.state import PaperVirtualLedger
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import OrderResultStatus, OrderSide, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.infra.polymarket.order_execution_types import OrderExecutionRequest


def _request(
    *,
    side: OrderSide,
    order_type: OrderType,
    amount_usdc: Decimal | None = None,
    size_shares: Decimal | None = None,
    price: Decimal | None = None,
) -> OrderExecutionRequest:
    return OrderExecutionRequest(
        strategy_id="sports_tail",
        action="submit",
        trace_id="trace1",
        idempotency_key="idem",
        condition_id="c1",
        token_id="t1",
        market_slug="m",
        side=side,
        order_type=order_type,
        price=price,
        amount_usdc=amount_usdc,
        size_shares=size_shares,
    )


def _orderbook(asks=(), bids=()) -> OrderbookSnapshot:
    ask_levels = tuple(PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks)
    bid_levels = tuple(PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids)
    return OrderbookSnapshot(
        token_id="t1",
        best_bid=bid_levels[0].price if bid_levels else None,
        best_ask=ask_levels[0].price if ask_levels else None,
        bids=bid_levels,
        asks=ask_levels,
        received_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )


def _market(fee_rate_bps: int | None = 30, fees_enabled: bool | None = True) -> Market:
    return Market(
        condition_id="c1",
        market_slug="m",
        outcomes=(),
        fee_rate_bps=fee_rate_bps,
        fees_enabled=fees_enabled,
    )


def test_buy_full_fill_records_fee_and_updates_ledger() -> None:
    ledger = PaperVirtualLedger()
    ledger.fund(Decimal("100"))
    request = _request(
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        amount_usdc=Decimal("10"),
        price=Decimal("0.55"),
    )
    outcome = simulate_fill(
        request,
        market=_market(fee_rate_bps=30),
        orderbook=_orderbook(asks=(("0.50", "100"),)),
        ledger=ledger,
    )
    assert outcome.response.status == OrderResultStatus.FULL_FILL
    assert outcome.match_result is not None
    assert outcome.match_result.filled_shares == Decimal("20")
    assert outcome.fee_quote is not None
    assert outcome.fee_quote.fee_usdc > Decimal("0")
    assert ledger.available_usdc == Decimal("90")
    assert ledger.position_for("t1") > Decimal("0")
    assert ledger.fees_accrued_usdc == outcome.fee_quote.fee_usdc


def test_buy_partial_fill_when_liquidity_short() -> None:
    ledger = PaperVirtualLedger()
    request = _request(
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        amount_usdc=Decimal("100"),
        price=Decimal("0.55"),
    )
    outcome = simulate_fill(
        request,
        market=_market(),
        orderbook=_orderbook(asks=(("0.50", "10"), ("0.55", "5"))),
        ledger=ledger,
    )
    # Filled 10 × 0.50 + 5 × 0.55 = 5 + 2.75 = 7.75 USDC; remaining 92.25
    assert outcome.response.status == OrderResultStatus.PARTIAL_FILL
    assert outcome.match_result is not None
    assert outcome.match_result.filled_shares == Decimal("15")
    assert outcome.match_result.unfilled_amount_usdc == Decimal("100") - Decimal("7.75")


def test_buy_no_fill_when_book_empty_returns_no_fill() -> None:
    ledger = PaperVirtualLedger()
    request = _request(
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        amount_usdc=Decimal("10"),
        price=Decimal("0.55"),
    )
    outcome = simulate_fill(
        request,
        market=_market(),
        orderbook=_orderbook(),
        ledger=ledger,
    )
    assert outcome.response.status == OrderResultStatus.NO_FILL
    assert ledger.available_usdc == Decimal("0")
    assert ledger.fees_accrued_usdc == Decimal("0")


def test_buy_no_fill_when_limit_below_all_asks() -> None:
    ledger = PaperVirtualLedger()
    request = _request(
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        amount_usdc=Decimal("10"),
        price=Decimal("0.40"),
    )
    outcome = simulate_fill(
        request,
        market=_market(),
        orderbook=_orderbook(asks=(("0.50", "100"),)),
        ledger=ledger,
    )
    assert outcome.response.status == OrderResultStatus.NO_FILL


def test_sell_gtc_returns_live_without_touching_ledger() -> None:
    ledger = PaperVirtualLedger()
    ledger.positions["t1"] = Decimal("100")
    request = _request(
        side=OrderSide.SELL,
        order_type=OrderType.GTC,
        size_shares=Decimal("50"),
        price=Decimal("0.95"),
    )
    outcome = simulate_fill(
        request,
        market=_market(),
        orderbook=_orderbook(bids=(("0.50", "100"),)),
        ledger=ledger,
    )
    assert outcome.response.status == OrderResultStatus.LIVE
    assert ledger.position_for("t1") == Decimal("100")
    assert ledger.fees_accrued_usdc == Decimal("0")


def test_sell_fak_full_fill_credits_net_proceeds() -> None:
    ledger = PaperVirtualLedger()
    ledger.positions["t1"] = Decimal("100")
    request = _request(
        side=OrderSide.SELL,
        order_type=OrderType.FAK,
        size_shares=Decimal("50"),
        price=Decimal("0.40"),
    )
    outcome = simulate_fill(
        request,
        market=_market(),
        orderbook=_orderbook(bids=(("0.45", "50"),)),
        ledger=ledger,
    )
    assert outcome.response.status == OrderResultStatus.FULL_FILL
    assert outcome.match_result is not None
    assert outcome.match_result.gross_notional_usdc == Decimal("22.50")
    assert outcome.fee_quote is not None
    assert ledger.available_usdc == Decimal("22.50") - outcome.fee_quote.fee_usdc
    assert ledger.position_for("t1") == Decimal("50")


def test_per_level_fee_aggregation_differs_from_avg_price_estimate() -> None:
    """跨 0.5 多档撮合：逐档求和的 fee 必须严格匹配 Polymarket 公式（避免 avg_price 单次估算偏差）。"""

    from decimal import Decimal as D

    from polymarket_trader.domain.fees import calculate_trade_fee

    ledger = PaperVirtualLedger()
    request = _request(
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        amount_usdc=D("100"),
        price=D("0.80"),
    )
    # 跨 0.5 的 ask 簿：0.40 / 0.60 / 0.80
    ob = OrderbookSnapshot(
        token_id="t1",
        best_bid=None,
        best_ask=D("0.40"),
        bids=(),
        asks=(
            PriceLevel(price=D("0.40"), size=D("50")),  # 20 USDC
            PriceLevel(price=D("0.60"), size=D("50")),  # 30 USDC
            PriceLevel(price=D("0.80"), size=D("100")),  # 50 USDC → 共 100
        ),
        received_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    market = _market(fee_rate_bps=30, fees_enabled=True)

    outcome = simulate_fill(request, market=market, orderbook=ob, ledger=ledger)
    assert outcome.fee_quote is not None
    assert outcome.match_result is not None

    # 手动按逐档求 fee 应等于 fill_engine 的聚合 fee
    expected_fee_usdc = D("0")
    for level in outcome.match_result.consumed_levels:
        per_level = calculate_trade_fee(
            price=level.price,
            size_shares=level.shares,
            side="buy",
            fee_rate_bps=30,
            fees_enabled=True,
            liquidity_role="taker",
            price_source="best_ask",
        )
        expected_fee_usdc += per_level.fee_usdc
    assert outcome.fee_quote.fee_usdc == expected_fee_usdc

    # 用 avg_price 单次估算与逐档求和的差值应非零（验证修正确实必要）
    avg_estimate = calculate_trade_fee(
        price=outcome.match_result.avg_price,
        size_shares=outcome.match_result.filled_shares,
        side="buy",
        fee_rate_bps=30,
        fees_enabled=True,
        liquidity_role="taker",
        price_source="best_ask",
    )
    assert avg_estimate.fee_usdc != expected_fee_usdc


def test_buy_response_uses_gross_matched_shares_per_production_semantics() -> None:
    """matched_shares 必须是 gross（与 production OrderResult 语义一致），fee 在 ledger 扣减。"""

    ledger = PaperVirtualLedger()
    request = _request(
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        amount_usdc=Decimal("10"),
        price=Decimal("0.55"),
    )
    outcome = simulate_fill(
        request,
        market=_market(fee_rate_bps=30, fees_enabled=True),
        orderbook=_orderbook(asks=(("0.50", "100"),)),
        ledger=ledger,
    )
    assert outcome.match_result is not None
    # response.matched_shares == gross；ledger.position == gross - fee_shares
    assert Decimal(str(outcome.response.matched_shares)) == outcome.match_result.filled_shares
    assert ledger.position_for("t1") < outcome.match_result.filled_shares
    assert ledger.position_for("t1") == outcome.match_result.filled_shares - (outcome.fee_quote.fee_shares or Decimal("0"))


def test_sell_response_spent_usdc_is_gross_per_production_semantics() -> None:
    """SELL 的 spent_usdc 应是 gross 名义额（与 order_result_builder 一致），fee 不扣进字段。"""

    ledger = PaperVirtualLedger()
    ledger.positions["t1"] = Decimal("100")
    ledger.cost_basis_usdc["t1"] = Decimal("40")
    request = _request(
        side=OrderSide.SELL,
        order_type=OrderType.FAK,
        size_shares=Decimal("50"),
        price=Decimal("0.40"),
    )
    outcome = simulate_fill(
        request,
        market=_market(fee_rate_bps=30, fees_enabled=True),
        orderbook=_orderbook(bids=(("0.45", "50"),)),
        ledger=ledger,
    )
    assert outcome.match_result is not None
    assert Decimal(str(outcome.response.spent_usdc)) == outcome.match_result.gross_notional_usdc
    # ledger 收到的是 net（扣 fee）
    assert ledger.available_usdc == outcome.match_result.gross_notional_usdc - outcome.fee_quote.fee_usdc


def test_market_with_fees_disabled_returns_zero_fee() -> None:
    ledger = PaperVirtualLedger()
    request = _request(
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        amount_usdc=Decimal("10"),
        price=Decimal("0.55"),
    )
    outcome = simulate_fill(
        request,
        market=_market(fees_enabled=False, fee_rate_bps=30),
        orderbook=_orderbook(asks=(("0.50", "100"),)),
        ledger=ledger,
    )
    assert outcome.response.status == OrderResultStatus.FULL_FILL
    assert outcome.fee_quote is not None
    assert outcome.fee_quote.fee_usdc == Decimal("0")
    assert ledger.fees_accrued_usdc == Decimal("0")
