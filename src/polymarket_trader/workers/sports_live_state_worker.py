"""体育直播状态同步 worker。

该 worker 是 P2 维护任务：从外部比分源读取状态，匹配当前跟踪 market，并写入
EntryMetadataStore 供策略入场评估读取。它不直接判断交易机会，也不绕过风控下单。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
    SportsLiveSnapshot,
    SportsLiveSourceStatus,
    SportsLiveSyncStatus,
)
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.serialization import jsonable

SportsLiveSnapshotProvider = Callable[[], Awaitable[SportsLiveSnapshot]]
SportsLiveResolvedMatch = tuple[Market, SportsLiveGame, Mapping[str, Any]]
SportsLiveStateMatcher = Callable[
    [Market, tuple[SportsLiveGame, ...]],
    SportsLiveResolvedMatch | None,
]

class SportsLiveMarketTracker(Protocol):
    """直播状态确认入场后，用于把 market 交给盘口热订阅的最小接口。"""

    def track_market(self, market: Market) -> None:
        """开始跟踪 market 的盘口快照。"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class SportsLiveSyncResult:
    """一次体育直播状态同步的结果摘要。"""

    source: str
    started_at: datetime
    completed_at: datetime
    games_seen: int
    markets_seen: int
    matches: int
    records_written: int
    unmatched_markets: int
    entry_signals_published: int
    source_statuses: tuple[SportsLiveSourceStatus, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


class SportsLiveStateWorker:
    """把外部体育直播状态同步到入场 metadata store。"""

    priority = "P2"

    def __init__(
        self,
        *,
        snapshot_provider: SportsLiveSnapshotProvider,
        match_live_state: SportsLiveStateMatcher,
        registry: MarketRegistry,
        entry_metadata_store: EntryMetadataStore,
        event_bus: EventBus | None = None,
        market_tracker: SportsLiveMarketTracker | Callable[[Market], None] | None = None,
        enabled: bool = True,
        source: str = "espn",
        leagues: tuple[str, ...] = (),
        publish_entry_signals: bool = True,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._match_live_state = match_live_state
        self._registry = registry
        self._entry_metadata_store = entry_metadata_store
        self._event_bus = event_bus
        self._market_tracker = market_tracker
        self._enabled = enabled
        self._source = source
        self._leagues = leagues
        self._entry_signal_publish_enabled = publish_entry_signals
        self._running = False
        self._last_started_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._last_games_seen = 0
        self._last_markets_seen = 0
        self._last_matches = 0
        self._last_records_written = 0
        self._last_unmatched_markets = 0
        self._last_entry_signals_published = 0
        self._last_source_statuses: tuple[SportsLiveSourceStatus, ...] = ()
        self._last_games: tuple[SportsLiveGame, ...] = ()

    async def sync_once(self) -> SportsLiveSyncResult | None:
        """执行一次同步；供 scheduler 和测试直接驱动。"""

        if not self._enabled:
            return None
        self._running = True
        self._last_started_at = _utc_now()
        self._last_error = None
        try:
            snapshot = await self._snapshot_provider()
            result = await self._apply_snapshot(snapshot, started_at=self._last_started_at)
        except Exception as exc:
            self._consecutive_failures += 1
            self._last_error = str(exc)
            self._last_completed_at = _utc_now()
            raise
        else:
            self._consecutive_failures = 0
            self._last_success_at = result.completed_at
            self._last_completed_at = result.completed_at
            self._last_games_seen = result.games_seen
            self._last_markets_seen = result.markets_seen
            self._last_matches = result.matches
            self._last_records_written = result.records_written
            self._last_unmatched_markets = result.unmatched_markets
            self._last_entry_signals_published = result.entry_signals_published
            self._last_source_statuses = result.source_statuses
            return result
        finally:
            self._running = False

    def last_games(self) -> tuple[SportsLiveGame, ...]:
        """返回最近一次成功同步的直播比赛集合，供高意图 discovery 只读使用。"""

        return self._last_games

    def status_snapshot(self) -> SportsLiveSyncStatus:
        """返回轻量运行态快照，不做 I/O。"""

        return SportsLiveSyncStatus(
            enabled=self._enabled,
            source=self._source,
            running=self._running,
            last_started_at=self._last_started_at,
            last_completed_at=self._last_completed_at,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
            last_games_seen=self._last_games_seen,
            last_markets_seen=self._last_markets_seen,
            last_matches=self._last_matches,
            last_records_written=self._last_records_written,
            last_unmatched_markets=self._last_unmatched_markets,
            last_entry_signals_published=self._last_entry_signals_published,
            leagues=self._leagues,
            source_statuses=self._last_source_statuses,
        )

    async def _apply_snapshot(
        self,
        snapshot: SportsLiveSnapshot,
        *,
        started_at: datetime,
    ) -> SportsLiveSyncResult:
        markets = self._registry.snapshot().markets
        matches = await self._match_markets(markets, snapshot.games)
        records_written = 0
        entry_signals = 0
        for market, game, metadata in matches:
            self._entry_metadata_store.upsert(
                condition_id=market.condition_id,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
                source=f"sports_live:{snapshot.source}",
                updated_at=snapshot.observed_at,
                metadata=metadata,
            )
            records_written += 1
            self._track_entry_signal_market(market=market, metadata=metadata)
            entry_signals += await self._publish_entry_signal_events(
                market=market,
                game=game,
                metadata=metadata,
            )

        completed_at = _utc_now()
        self._last_games = snapshot.games
        return SportsLiveSyncResult(
            source=snapshot.source,
            started_at=started_at,
            completed_at=completed_at,
            games_seen=len(snapshot.games),
            markets_seen=len(markets),
            matches=len(matches),
            records_written=records_written,
            unmatched_markets=max(0, len(markets) - len(matches)),
            entry_signals_published=entry_signals,
            source_statuses=snapshot.source_statuses,
        )

    async def _match_markets(
        self,
        markets: tuple[Market, ...],
        games: tuple[SportsLiveGame, ...],
    ) -> tuple[SportsLiveResolvedMatch, ...]:
        results: list[SportsLiveResolvedMatch] = []
        for index, market in enumerate(markets, start=1):
            match = self._match_live_state(market, games)
            if match is not None:
                results.append(match)
            if index % 10 == 0:
                # 单轮同步可能需要做 markets x games 的文本匹配；P2 任务必须让出事件循环。
                await asyncio.sleep(0)
        return tuple(results)

    def _track_entry_signal_market(
        self,
        *,
        market: Market,
        metadata: Mapping[str, Any],
    ) -> None:
        """把直播确认可进场的市场交给盘口 WS 跟踪，保证后续决策读取热盘口。"""

        if self._market_tracker is None:
            return
        if metadata.get("sports_tail_entry_signal_allowed") is False:
            return
        tracker = self._market_tracker
        if callable(tracker):
            tracker(market)
            return
        track_market = getattr(tracker, "track_market", None)
        if callable(track_market):
            track_market(market)

    async def _publish_entry_signal_events(
        self,
        *,
        market: Market,
        game: SportsLiveGame,
        metadata: Mapping[str, Any],
    ) -> int:
        if not self._entry_signal_publish_enabled or self._event_bus is None:
            return 0
        if metadata.get("sports_tail_entry_signal_allowed") is False:
            return 0
        count = 0
        for token_id in market.token_ids:
            await self._event_bus.publish(
                OutboxPriority.P1,
                DomainEvent(
                    trace_id=f"sports-live-state-{uuid4().hex}",
                    event_type=DomainEventType.ENTRY_SIGNAL_TRIGGERED,
                    event_id=uuid4().hex,
                    market_slug=market.market_slug,
                    event_slug=market.event_slug,
                    condition_id=market.condition_id,
                    token_id=token_id,
                    reason="sports_live_state_updated",
                    payload={
                        "origin": "sports_live_state_worker",
                        "source": game.source,
                        "source_event_id": game.source_event_id,
                        "match": jsonable(metadata.get("sports_live_match")),
                    },
                ),
            )
            count += 1
        return count
