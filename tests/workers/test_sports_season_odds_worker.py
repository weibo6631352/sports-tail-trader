from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.sports_season_odds_worker import (
    SportsSeasonOddsWorker,
)


def _market() -> Market:
    return Market(
        condition_id="nba-champion-2026",
        market_slug="will-celtics-win-2026-nba-championship",
        market_question="Will Celtics win 2026 NBA championship?",
        event_title="2026 NBA Champion",
        event_slug="2026-nba-championship-winner",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


class _FakeOddsClient:
    def __init__(self, snapshot: SeasonOddsSnapshot | None) -> None:
        self._snapshot = snapshot

    async def fetch(self, *, sport_key: str, market_key: str) -> SeasonOddsSnapshot | None:
        return self._snapshot

    async def aclose(self) -> None:
        return None


def test_odds_worker_disabled_skips_work() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    snapshot = SeasonOddsSnapshot(
        market_key="x",
        fair_probabilities={"Yes": Decimal("0.4")},
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        source="theoddsapi",
    )
    worker = SportsSeasonOddsWorker(
        odds_client=_FakeOddsClient(snapshot),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "basketball_nba",
        is_outright_market=lambda _m: True,
        market_key_for=lambda m: m.event_slug,
        enabled=False,
    )
    refreshed = asyncio.run(worker.sync_once())
    assert refreshed == 0
    assert store.records() == ()


def test_odds_worker_persists_snapshot_and_preserves_live_state() -> None:
    """关键回归：写 season_odds 不能清掉 live_state worker 设置的 live_state_* 字段。"""

    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    # 模拟 live_state worker 先写入了一份 single_game 元数据。
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
    snapshot = SeasonOddsSnapshot(
        market_key=market.event_slug,
        fair_probabilities={"Yes": Decimal("0.40"), "No": Decimal("0.60")},
        observed_at=datetime(2026, 5, 11, 12, tzinfo=timezone.utc),
        source="theoddsapi",
    )
    worker = SportsSeasonOddsWorker(
        odds_client=_FakeOddsClient(snapshot),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "basketball_nba",
        is_outright_market=lambda _m: True,
        market_key_for=lambda m: m.event_slug,
        ttl_seconds=60,
        enabled=True,
    )
    refreshed = asyncio.run(worker.sync_once())
    assert refreshed == 1
    record = store.find(condition_id=market.condition_id)
    assert record is not None
    # season_odds_snapshot 写入；既有 metadata 字段保留
    assert "season_odds_snapshot" in record.metadata
    assert record.metadata["unrelated"] == "value"
    # live_state_* 字段必须没有被清空
    assert record.live_state_signal_allowed is True
    assert record.live_state_signal_reason == "within_tail_window"
    assert record.live_state_phase == "live"
    assert record.live_state_payload == {"live_game": {"foo": "bar"}}


def test_odds_worker_respects_ttl_throttle() -> None:
    registry = MarketRegistry()
    store = EntryMetadataStore()
    market = _market()
    registry.upsert(market)
    snapshot = SeasonOddsSnapshot(
        market_key=market.event_slug,
        fair_probabilities={"Yes": Decimal("0.30")},
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        source="theoddsapi",
    )
    worker = SportsSeasonOddsWorker(
        odds_client=_FakeOddsClient(snapshot),
        registry=registry,
        entry_metadata_store=store,
        sport_key_for=lambda _m: "basketball_nba",
        is_outright_market=lambda _m: True,
        market_key_for=lambda m: m.event_slug,
        ttl_seconds=3600,  # 1 hour
        enabled=True,
    )
    first = asyncio.run(worker.sync_once())
    second = asyncio.run(worker.sync_once())
    # 第一次拉，第二次因 TTL 节流不重复
    assert first == 1
    assert second == 0
