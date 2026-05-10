"""体育直播状态同步 worker。

该 worker 是 P2 维护任务：从外部比分源读取状态，匹配当前跟踪 market，并写入
EntryMetadataStore 供策略入场评估读取。它不直接判断交易机会，也不绕过风控下单。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
from polymarket_trader.extension_api.lifecycle import LifecycleEvent
from polymarket_trader.extension_api.live_state import LiveStateMatch
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.lifecycle_bus import LifecyclePublisher
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.serialization import jsonable

SportsLiveSnapshotProvider = Callable[[], Awaitable[SportsLiveSnapshot]]
SportsLiveStateMatcher = Callable[
    [Market, tuple[SportsLiveGame, ...]],
    LiveStateMatch | None,
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
        lifecycle_bus: LifecyclePublisher | None = None,
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
        self._lifecycle_bus = lifecycle_bus
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
        for match in matches:
            market = match.market
            self._entry_metadata_store.upsert(
                condition_id=market.condition_id,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
                source=f"sports_live:{snapshot.source}",
                updated_at=snapshot.observed_at,
                metadata=dict(match.payload),
                live_state_signal_allowed=match.signal_allowed,
                live_state_signal_reason=match.signal_reason,
                live_state_phase=match.phase,
                live_state_payload=dict(match.payload),
            )
            records_written += 1
            self._track_entry_signal_market(match=match)
            entry_signals += await self._publish_entry_signal_events(match=match)
            self._publish_live_state_lifecycle(match=match)

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
    ) -> tuple[LiveStateMatch, ...]:
        results: list[LiveStateMatch] = []
        for index, market in enumerate(markets, start=1):
            match = self._match_live_state(market, games)
            if match is not None:
                results.append(match)
            if index % 10 == 0:
                # 单轮同步可能需要做 markets x games 的文本匹配；P2 任务必须让出事件循环。
                await asyncio.sleep(0)
        return tuple(results)

    def _publish_live_state_lifecycle(self, *, match: LiveStateMatch) -> None:
        """让策略可订阅 LIVE_STATE_UPDATED 触发自家健康检查 / 信号缓存刷新。"""

        if self._lifecycle_bus is None:
            return
        market = match.market
        self._lifecycle_bus.publish(
            LifecycleEvent.LIVE_STATE_UPDATED,
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            payload={
                "source": match.game.source,
                "source_event_id": match.game.source_event_id,
                "signal_allowed": match.signal_allowed,
                "signal_reason": match.signal_reason,
                "phase": match.phase,
                "payload": dict(match.payload),
            },
        )

    def _track_entry_signal_market(self, *, match: LiveStateMatch) -> None:
        """把直播确认可进场的市场交给盘口 WS 跟踪，保证后续决策读取热盘口。"""

        if self._market_tracker is None:
            return
        if not match.signal_allowed:
            return
        tracker = self._market_tracker
        if callable(tracker):
            tracker(match.market)
            return
        track_market = getattr(tracker, "track_market", None)
        if callable(track_market):
            track_market(match.market)

    async def _publish_entry_signal_events(self, *, match: LiveStateMatch) -> int:
        if not self._entry_signal_publish_enabled or self._event_bus is None:
            return 0
        if not match.signal_allowed:
            return 0
        market = match.market
        game = match.game
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
                        "match": jsonable(match.payload),
                        "signal_reason": match.signal_reason,
                    },
                ),
            )
            count += 1
        return count
