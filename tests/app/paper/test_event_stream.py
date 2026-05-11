"""沙箱事件流 JSONL 序列化与排序测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from polymarket_trader.app.paper.event_stream import (
    RecordedEventStream,
    ShadowEvent,
    SyntheticEventStream,
    dump_shadow_events_to_jsonl,
    load_shadow_events_from_jsonl,
)
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel


def _market() -> Market:
    return Market(
        condition_id="c1",
        market_slug="nhl-tb-mon-totals",
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
        fee_rate_bps=30,
    )


def _orderbook(observed_at: datetime) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id="over",
        best_bid=Decimal("0.97"),
        best_ask=Decimal("0.98"),
        bids=(PriceLevel(price=Decimal("0.97"), size=Decimal("20")),),
        asks=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
        received_at=observed_at,
        market_slug="nhl-tb-mon-totals",
        condition_id="c1",
        tick_size=Decimal("0.01"),
    )


def _event(observed_at: datetime, trace: str) -> ShadowEvent:
    return ShadowEvent(
        event_type="orderbook_snapshot_updated",
        observed_at=observed_at,
        condition_id="c1",
        token_id="over",
        market_slug="nhl-tb-mon-totals",
        trace_id=trace,
        market=_market(),
        orderbook=_orderbook(observed_at),
        live_metadata={"league": "NHL", "home_score": 3, "away_score": 2},
    )


def test_jsonl_roundtrip_preserves_fields(tmp_path: Path) -> None:
    t0 = datetime(2026, 5, 11, tzinfo=timezone.utc)
    events = (_event(t0, "trace-a"), _event(t0 + timedelta(seconds=30), "trace-b"))
    target = tmp_path / "shadow.jsonl"

    written = dump_shadow_events_to_jsonl(events, target)
    assert written == 2

    reloaded = tuple(load_shadow_events_from_jsonl(target))
    assert len(reloaded) == 2
    assert reloaded[0].trace_id == "trace-a"
    assert reloaded[0].market.fee_rate_bps == 30
    assert reloaded[0].market.outcomes[0].token_id == "over"
    assert reloaded[0].orderbook.best_ask == Decimal("0.98")
    assert reloaded[0].orderbook.asks[0].size == Decimal("20")
    assert reloaded[0].live_metadata == {"league": "NHL", "home_score": 3, "away_score": 2}


def test_recorded_stream_sorts_by_observed_at(tmp_path: Path) -> None:
    t0 = datetime(2026, 5, 11, tzinfo=timezone.utc)
    out_of_order = (
        _event(t0 + timedelta(seconds=60), "later"),
        _event(t0, "first"),
        _event(t0 + timedelta(seconds=30), "middle"),
    )
    target = tmp_path / "ooo.jsonl"
    dump_shadow_events_to_jsonl(out_of_order, target)

    stream = RecordedEventStream(target)
    traces = tuple(event.trace_id for event in stream)
    assert traces == ("first", "middle", "later")


def test_synthetic_stream_yields_in_order() -> None:
    t0 = datetime(2026, 5, 11, tzinfo=timezone.utc)
    events = (_event(t0, "a"), _event(t0 + timedelta(seconds=10), "b"))
    stream = SyntheticEventStream(events)
    assert tuple(event.trace_id for event in stream) == ("a", "b")
