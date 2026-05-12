"""series_state_worker：mock client → 写入 metadata + 保留 live_state 字段 + TTL 节流。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.series_state_worker import SeriesStateWorker
from strategies.current.series.types import SeriesState


_OBS = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def _market() -> Market:
    return Market(
        condition_id="cond-series",
        market_slug="nba-2026-celtics-knicks-series",
        market_question="Who wins the series?",
        event_slug="2026-celtics-knicks-series",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="tok-yes", outcome="Yes"),
            MarketOutcome(token_id="tok-no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


class _FakeClient:
    def __init__(self, state: SeriesState | None) -> None:
        self._state = state

    async def fetch(self, *, sport_key: str, series_key: str, observed_at=None):
        return self._state

    async def aclose(self) -> None:
        return None


def _state() -> SeriesState:
    return SeriesState(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        wins_a=2,
        wins_b=1,
        best_of=7,
        next_game_at=None,
        observed_at=_OBS,
    )


def test_disabled_worker_does_nothing() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    worker = SeriesStateWorker(
        client=_FakeClient(_state()),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "nba",
        is_series_winner_market=lambda _m: True,
        series_key_for=lambda m: m.event_slug,
        enabled=False,
    )
    refreshed = asyncio.run(worker.sync_once())
    assert refreshed == 0
    assert store.records() == ()


def test_writes_series_state_metadata_and_preserves_live_state() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    # 模拟 live_state worker 先写入了 live_state 字段。
    store.upsert(
        condition_id=market.condition_id,
        market_slug=market.market_slug,
        event_slug=market.event_slug,
        source="unit_test_live",
        metadata={"unrelated": "value"},
        live_state_signal_allowed=True,
        live_state_signal_reason="within_tail_window",
        live_state_phase="live",
        live_state_payload={"live_game": {"foo": "bar"}},
    )
    worker = SeriesStateWorker(
        client=_FakeClient(_state()),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "nba",
        is_series_winner_market=lambda _m: True,
        series_key_for=lambda m: m.event_slug,
        ttl_seconds=60,
        enabled=True,
    )
    refreshed = asyncio.run(worker.sync_once())
    assert refreshed == 1
    record = store.find(condition_id=market.condition_id)
    assert record is not None
    assert "series_state" in record.metadata
    assert record.metadata["series_state"]["team_a"] == "Boston Celtics"
    assert record.metadata["series_state"]["wins_a"] == 2
    assert record.metadata["unrelated"] == "value"
    # live_state_* 字段必须没有被清空
    assert record.live_state_signal_allowed is True
    assert record.live_state_signal_reason == "within_tail_window"
    assert record.live_state_phase == "live"
    assert record.live_state_payload == {"live_game": {"foo": "bar"}}


def test_ttl_throttle_skips_repeat_fetch() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    worker = SeriesStateWorker(
        client=_FakeClient(_state()),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "nba",
        is_series_winner_market=lambda _m: True,
        series_key_for=lambda m: m.event_slug,
        ttl_seconds=3600,
        enabled=True,
    )
    first = asyncio.run(worker.sync_once())
    second = asyncio.run(worker.sync_once())
    assert first == 1
    assert second == 0


def test_skips_markets_not_classified_as_series_winner() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    worker = SeriesStateWorker(
        client=_FakeClient(_state()),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "nba",
        is_series_winner_market=lambda _m: False,
        series_key_for=lambda m: m.event_slug,
        enabled=True,
    )
    refreshed = asyncio.run(worker.sync_once())
    assert refreshed == 0


def test_client_returning_none_does_not_write_metadata() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    worker = SeriesStateWorker(
        client=_FakeClient(None),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "nba",
        is_series_winner_market=lambda _m: True,
        series_key_for=lambda m: m.event_slug,
        enabled=True,
    )
    refreshed = asyncio.run(worker.sync_once())
    assert refreshed == 0
    assert store.records() == ()
