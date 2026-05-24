"""赛季隐含概率同步 worker（P2，低频）。

针对追踪的 outright market 周期性拉取赛季 fair probabilities，写入
``EntryMetadataStore`` 的 ``season_odds_snapshot`` 键。outright 评估器在决策
路径读这份内存快照、不发起 IO。

cadence 默认 1800s。每个 market 内部按 ``ttl_seconds`` 节流，避免免费 API
配额耗尽。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from polymarket_trader.infra.sports.season_odds_client import SeasonOddsClient
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.serialization import jsonable

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


SportKeyResolver = Callable[[Market], str | None]


class SportsSeasonOddsWorker:
    """周期性拉取追踪 outright market 的赛季隐含概率。"""

    priority = "P2"

    def __init__(
        self,
        *,
        odds_client: SeasonOddsClient,
        registry: MarketRegistry,
        entry_metadata_store: EntryMetadataStore,
        sport_key_for: SportKeyResolver,
        is_outright_market: Callable[[Market], bool],
        market_key_for: Callable[[Market], str],
        ttl_seconds: int = 1800,
        enabled: bool = False,
        event_bus: EventBus | None = None,
    ) -> None:
        self._odds_client = odds_client
        self._registry = registry
        self._entry_metadata_store = entry_metadata_store
        self._sport_key_for = sport_key_for
        self._is_outright_market = is_outright_market
        self._market_key_for = market_key_for
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._enabled = enabled
        self._event_bus = event_bus
        self._last_fetched_at: dict[str, datetime] = {}
        self._last_started_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0

    def evict_market(self, condition_id: str, token_ids: tuple[str, ...]) -> None:
        """registry prune callback:清 fetch 时间戳,防 cid 累积."""
        self._last_fetched_at.pop(condition_id, None)
        self._last_markets_seen = 0
        self._last_markets_refreshed = 0

    async def sync_once(self) -> int:
        """对追踪 outright 市场各拉一次。返回 fresh snapshot 写入次数。"""

        if not self._enabled:
            return 0
        self._last_started_at = _utc_now()
        self._last_error = None
        markets = self._registry.snapshot().markets
        outrights = tuple(m for m in markets if self._is_outright_market(m))
        self._last_markets_seen = len(outrights)
        refreshed = 0
        for market in outrights:
            sport_key = self._sport_key_for(market)
            if not sport_key:
                continue
            market_key = self._market_key_for(market)
            if not self._should_refresh(market.condition_id):
                continue
            try:
                snapshot = await self._odds_client.fetch(sport_key=sport_key, market_key=market_key)
            except Exception as exc:
                self._consecutive_failures += 1
                self._last_error = f"{market.market_slug}: {exc}"
                logger.warning("season_odds_fetch_failed", extra={"market_slug": market.market_slug, "error": str(exc), "consecutive_failures": self._consecutive_failures})
                continue
            self._consecutive_failures = 0
            self._last_fetched_at[market.condition_id] = _utc_now()
            if snapshot is None:
                continue
            self._upsert(market, snapshot)
            await self._publish_entry_signals(market)
            refreshed += 1
        self._last_markets_refreshed = refreshed
        return refreshed

    async def _publish_entry_signals(self, market: Market) -> None:
        if self._event_bus is None:
            return
        for token_id in market.token_ids:
            await self._event_bus.publish(
                OutboxPriority.P1,
                DomainEvent(
                    trace_id=f"season-odds-{uuid4().hex}",
                    event_type=DomainEventType.ENTRY_SIGNAL_TRIGGERED,
                    event_id=uuid4().hex,
                    market_slug=market.market_slug,
                    event_slug=market.event_slug,
                    condition_id=market.condition_id,
                    token_id=token_id,
                    reason="season_odds_refreshed",
                    payload={"origin": "season_odds_worker"},
                ),
            )

    def _should_refresh(self, condition_id: str) -> bool:
        last = self._last_fetched_at.get(condition_id)
        if last is None:
            return True
        return (_utc_now() - last).total_seconds() >= self._ttl_seconds

    def _upsert(self, market: Market, snapshot: SeasonOddsSnapshot) -> None:
        # EntryMetadataStore.upsert 是整记录替换，必须保留既有 live_state_* 字段，
        # 否则 outright 的 odds 写入会清掉 live_state worker 的 single_game 数据。
        payload = dict(jsonable(snapshot))
        existing_record = self._entry_metadata_store.find(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
        )
        existing_metadata: dict = {}
        existing_live_state_allowed = None
        existing_live_state_reason = ""
        existing_live_state_phase = ""
        existing_live_state_payload: dict = {}
        if existing_record is not None:
            existing_metadata = dict(existing_record.metadata)
            existing_live_state_allowed = existing_record.live_state_signal_allowed
            existing_live_state_reason = existing_record.live_state_signal_reason or ""
            existing_live_state_phase = existing_record.live_state_phase or ""
            existing_live_state_payload = dict(existing_record.live_state_payload or {})
        existing_metadata["season_odds_snapshot"] = payload
        self._entry_metadata_store.upsert(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
            source=f"season_odds:{snapshot.source}",
            updated_at=snapshot.observed_at,
            metadata=existing_metadata,
            live_state_signal_allowed=existing_live_state_allowed,
            live_state_signal_reason=existing_live_state_reason,
            live_state_phase=existing_live_state_phase,
            live_state_payload=existing_live_state_payload,
        )

    def status_snapshot(self) -> Mapping[str, object]:
        return {
            "enabled": self._enabled,
            "last_started_at": self._last_started_at.isoformat() if self._last_started_at else None,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
            "last_markets_seen": self._last_markets_seen,
            "last_markets_refreshed": self._last_markets_refreshed,
            "ttl_seconds": self._ttl_seconds,
        }
