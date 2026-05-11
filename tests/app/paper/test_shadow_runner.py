"""Shadow run 集成测试：决策序列 + 累计 ledger + 确定性回归。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_trader.app.paper import (
    PaperVirtualLedger,
    ShadowEvent,
    SyntheticEventStream,
    run_shadow_session,
)
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel

from strategies.current.config import CurrentStrategyConfig
from strategies.current.strategy import CurrentStrategy


T0 = datetime(2026, 4, 27, 22, 0, 0, tzinfo=timezone.utc)


def _market() -> Market:
    return Market(
        condition_id="totals-condition",
        market_slug="nhl-tb-mon-total-4-5",
        market_question="TB vs MON total over/under 4.5",
        event_title="TB vs MON",
        event_slug="nhl-tb-mon",
        category="Sports",
        tags=("NHL",),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _orderbook(observed_at: datetime) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id="over",
        best_bid=Decimal("0.97"),
        best_ask=Decimal("0.98"),
        bids=(PriceLevel(price=Decimal("0.97"), size=Decimal("20")),),
        asks=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
        received_at=observed_at,
        market_slug="nhl-tb-mon-total-4-5",
        condition_id="totals-condition",
        tick_size=Decimal("0.01"),
    )


def _event(observed_at: datetime, trace: str) -> ShadowEvent:
    return ShadowEvent(
        event_type="orderbook_snapshot_updated",
        observed_at=observed_at,
        condition_id="totals-condition",
        token_id="over",
        market_slug="nhl-tb-mon-total-4-5",
        trace_id=trace,
        market=_market(),
        orderbook=_orderbook(observed_at),
        live_metadata={
            "league": "NHL",
            "home_name": "TB",
            "away_name": "MON",
            "home_score": 3,
            "away_score": 2,
            "period": "P3",
            "seconds_remaining": 420,
            "status": "live",
            "observed_at": observed_at.isoformat(),
            "source": "shadow_test",
        },
    )


def _run(events: tuple[ShadowEvent, ...], *, ledger: PaperVirtualLedger | None = None):
    ledger = ledger or PaperVirtualLedger()
    return asyncio.run(
        run_shadow_session(
            SyntheticEventStream(events),
            extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
            ledger=ledger,
            starting_balance_usdc=Decimal("10"),
            portfolio_budget_usdc=Decimal("10"),
            max_order_usdc=Decimal("10"),
            max_market_usdc=Decimal("10"),
            max_total_usdc=Decimal("10"),
        )
    ), ledger


def test_single_event_triggers_buy_fill_and_updates_ledger() -> None:
    events = (_event(T0, "trace-1"),)
    report, ledger = _run(events)

    assert report.events_processed == 1
    assert report.entry_fills == 1
    assert report.timeouts == 0
    assert len(report.frames) == 1
    frame = report.frames[0]
    assert frame.error is None
    assert frame.entry_order_status == "full_fill"
    # 10 USDC × ask 0.98 → 撮合 10/0.98 ≈ 10.204 shares；账户花光 10
    assert Decimal(frame.entry_spent_usdc) == Decimal("10")
    assert ledger.available_usdc == Decimal("0")
    assert ledger.position_for("over") > Decimal("10")
    assert ledger.cost_for("over") == Decimal("10")


def test_multiple_events_accumulate_in_ledger() -> None:
    events = (
        _event(T0, "trace-a"),
        _event(T0 + timedelta(seconds=30), "trace-b"),
        _event(T0 + timedelta(seconds=60), "trace-c"),
    )
    report, ledger = _run(events)

    assert report.events_processed == 3
    # 第一笔 BUY 用完预算后，后续事件不应再消耗 USDC
    assert ledger.available_usdc >= Decimal("-0.0001")
    # 至少一次成交
    assert ledger.position_for("over") > Decimal("0")


def test_shadow_run_is_deterministic_on_same_stream() -> None:
    events = (_event(T0, "trace-1"), _event(T0 + timedelta(seconds=30), "trace-2"))

    report_a, ledger_a = _run(events)
    report_b, ledger_b = _run(events)

    assert report_a.events_processed == report_b.events_processed
    assert report_a.entry_fills == report_b.entry_fills
    assert report_a.resting_orders_placed == report_b.resting_orders_placed
    assert report_a.timeouts == report_b.timeouts
    assert ledger_a.available_usdc == ledger_b.available_usdc
    assert ledger_a.fees_accrued_usdc == ledger_b.fees_accrued_usdc
    assert ledger_a.positions == ledger_b.positions
    assert ledger_a.cost_basis_usdc == ledger_b.cost_basis_usdc
    for frame_a, frame_b in zip(report_a.frames, report_b.frames, strict=True):
        assert frame_a.entry_order_status == frame_b.entry_order_status
        assert frame_a.entry_filled_shares == frame_b.entry_filled_shares
        assert frame_a.entry_spent_usdc == frame_b.entry_spent_usdc
        assert frame_a.available_usdc_after == frame_b.available_usdc_after
        assert frame_a.fees_accrued_after == frame_b.fees_accrued_after
        assert frame_a.error == frame_b.error


def test_empty_stream_returns_empty_report() -> None:
    report, ledger = _run(())
    assert report.events_processed == 0
    assert report.entry_fills == 0
    assert report.resting_orders_placed == 0
    assert report.timeouts == 0
    assert ledger.available_usdc == Decimal("10")  # 沙箱按 starting_balance 兜底注资
