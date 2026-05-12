"""game_odds_worker：mock client → 写入 metadata + 保留 live_state + 不破坏 series_state。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.infra.sports.game_odds_client import GameOddsSnapshot
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.game_odds_worker import GameOddsWorker


_OBS = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def _market() -> Market:
    return Market(
        condition_id="cond-series",
        market_slug="nba-celtics-knicks-series",
        market_question="Will Celtics win the series?",
        event_slug="celtics-vs-knicks-series",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="tok-yes", outcome="Yes"),
            MarketOutcome(token_id="tok-no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


class _FakeClient:
    def __init__(self, snapshot: GameOddsSnapshot | None) -> None:
        self._snapshot = snapshot

    async def fetch(self, *, sport_key: str, game_key: str):
        return self._snapshot

    async def aclose(self) -> None:
        return None


def _snapshot() -> GameOddsSnapshot:
    return GameOddsSnapshot(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        p_a=Decimal("0.62"),
        observed_at=_OBS,
        source="theoddsapi",
    )


def test_writes_game_odds_and_preserves_existing_metadata() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    # 模拟 series_state worker 先写入了 series_state metadata。
    store.upsert(
        condition_id=market.condition_id,
        market_slug=market.market_slug,
        event_slug=market.event_slug,
        source="series_state:test",
        metadata={"series_state": {"team_a": "X"}},
        live_state_signal_allowed=True,
        live_state_signal_reason="reason",
        live_state_phase="live",
        live_state_payload={"k": "v"},
    )
    worker = GameOddsWorker(
        client=_FakeClient(_snapshot()),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "basketball_nba",
        is_series_winner_market=lambda _m: True,
        game_key_for=lambda m: m.event_slug,
        ttl_seconds=60,
        enabled=True,
    )
    refreshed = asyncio.run(worker.sync_once())
    assert refreshed == 1
    record = store.find(condition_id=market.condition_id)
    assert record is not None
    assert "game_odds" in record.metadata
    assert record.metadata["game_odds"]["p_a"] == "0.62"
    # series_state 没被清掉
    assert record.metadata["series_state"] == {"team_a": "X"}
    # live_state 字段保留
    assert record.live_state_signal_allowed is True
    assert record.live_state_phase == "live"


def test_disabled_worker_does_nothing() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    worker = GameOddsWorker(
        client=_FakeClient(_snapshot()),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "basketball_nba",
        is_series_winner_market=lambda _m: True,
        game_key_for=lambda m: m.event_slug,
        enabled=False,
    )
    refreshed = asyncio.run(worker.sync_once())
    assert refreshed == 0


def test_ttl_throttles_repeat_fetch() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    worker = GameOddsWorker(
        client=_FakeClient(_snapshot()),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "basketball_nba",
        is_series_winner_market=lambda _m: True,
        game_key_for=lambda m: m.event_slug,
        ttl_seconds=3600,
        enabled=True,
    )
    first = asyncio.run(worker.sync_once())
    second = asyncio.run(worker.sync_once())
    assert first == 1
    assert second == 0
