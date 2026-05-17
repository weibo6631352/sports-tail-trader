"""单场胜率同步 worker（P2，低频）。

针对追踪的 series WINNER market 周期性拉取下一场比赛 moneyline，写入
``EntryMetadataStore`` 的 ``game_odds`` 键。series evaluator 在决策路径读
这份内存快照，不发起 IO。

cadence 与 series_state 一致（默认 600s），但底层 TheOddsAPI 配额较紧，
每个 market 内部按 ttl_seconds 节流（默认 1800s）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

import logging

from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.infra.sports.game_odds_client import (
    GameOddsClient,
    GameOddsSnapshot,
    GameSpreadSnapshot,
)
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


SportKeyResolver = Callable[[Market], str | None]
GameKeyResolver = Callable[[Market], str | None]


class GameOddsWorker:
    """周期拉取追踪 series winner market 的下一场 h2h 胜率。"""

    priority = "P2"

    def __init__(
        self,
        *,
        client: GameOddsClient,
        registry: MarketRegistry,
        entry_metadata_store: EntryMetadataStore,
        sport_key_for: SportKeyResolver,
        is_series_winner_market: Callable[[Market], bool],
        game_key_for: GameKeyResolver,
        ttl_seconds: int = 1800,
        enabled: bool = False,
        event_bus: EventBus | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._entry_metadata_store = entry_metadata_store
        self._sport_key_for = sport_key_for
        self._is_series_winner_market = is_series_winner_market
        self._game_key_for = game_key_for
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._enabled = enabled
        self._event_bus = event_bus
        self._last_fetched_at: dict[str, datetime] = {}
        self._last_started_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._last_markets_seen = 0
        self._last_markets_refreshed = 0

    async def sync_once(self) -> int:
        if not self._enabled:
            return 0
        self._last_started_at = _utc_now()
        self._last_error = None
        markets = self._registry.snapshot().markets
        targets = tuple(m for m in markets if self._is_series_winner_market(m))
        self._last_markets_seen = len(targets)
        refreshed = 0
        for market in targets:
            sport_key = self._sport_key_for(market)
            if not sport_key:
                continue
            game_key = self._game_key_for(market)
            if not game_key:
                continue
            if not self._should_refresh(market.condition_id):
                continue
            try:
                snapshot = await self._client.fetch(
                    sport_key=sport_key,
                    game_key=game_key,
                )
            except Exception as exc:
                self._consecutive_failures += 1
                self._last_error = f"{market.market_slug}: {exc}"
                logger.warning("game_odds_fetch_failed", extra={"market_slug": market.market_slug, "error": str(exc), "consecutive_failures": self._consecutive_failures})
                continue
            # spreads 缺失不影响 h2h 主路径：series WINNER 仍可用 h2h；只在
            # HANDICAP single_game scope 缺数据时由 evaluator 报 MISSING_GAME_SPREADS。
            spread_snapshot: GameSpreadSnapshot | None = None
            try:
                spread_snapshot = await self._client.fetch_spreads(
                    sport_key=sport_key,
                    game_key=game_key,
                )
            except Exception as exc:
                self._last_error = f"spreads:{market.market_slug}: {exc}"
                logger.warning("game_spreads_fetch_failed", extra={"market_slug": market.market_slug, "error": str(exc)})
            self._consecutive_failures = 0
            self._last_fetched_at[market.condition_id] = _utc_now()
            if snapshot is None and spread_snapshot is None:
                continue
            self._upsert(market, snapshot, spread_snapshot)
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
                    trace_id=f"game-odds-{uuid4().hex}",
                    event_type=DomainEventType.ENTRY_SIGNAL_TRIGGERED,
                    event_id=uuid4().hex,
                    market_slug=market.market_slug,
                    event_slug=market.event_slug,
                    condition_id=market.condition_id,
                    token_id=token_id,
                    reason="game_odds_refreshed",
                    merge_key=f"orderbook_snapshot_updated|{token_id}",
                    payload={"origin": "game_odds_worker"},
                ),
            )

    def _should_refresh(self, condition_id: str) -> bool:
        last = self._last_fetched_at.get(condition_id)
        if last is None:
            return True
        return (_utc_now() - last).total_seconds() >= self._ttl_seconds

    def _upsert(
        self,
        market: Market,
        snapshot: GameOddsSnapshot | None,
        spread_snapshot: GameSpreadSnapshot | None,
    ) -> None:
        # 与 series_state_worker 同模式：整记录替换需保留既有 metadata 字段，
        # 否则会清掉 live_state / season_odds / series_state 字段。
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
        if snapshot is not None:
            payload: dict = {
                "team_a": snapshot.team_a,
                "team_b": snapshot.team_b,
                "p_a": str(snapshot.p_a),
                "observed_at": snapshot.observed_at.isoformat(),
                "source": snapshot.source,
            }
            if snapshot.source_event_id:
                payload["source_event_id"] = snapshot.source_event_id
            existing_metadata["game_odds"] = payload
        if spread_snapshot is not None:
            spread_payload: dict = {
                "team_a": spread_snapshot.team_a,
                "team_b": spread_snapshot.team_b,
                "spread_line": str(spread_snapshot.spread_line),
                "p_a_covers": str(spread_snapshot.p_a_covers),
                "observed_at": spread_snapshot.observed_at.isoformat(),
                "source": spread_snapshot.source,
            }
            if spread_snapshot.source_event_id:
                spread_payload["source_event_id"] = spread_snapshot.source_event_id
            existing_metadata["game_spreads"] = spread_payload
        # source 标签取 h2h 优先，spreads 兜底；observed_at 同理。
        primary = snapshot or spread_snapshot
        if primary is None:
            return
        self._entry_metadata_store.upsert(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
            source=f"game_odds:{primary.source}",
            updated_at=primary.observed_at,
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
