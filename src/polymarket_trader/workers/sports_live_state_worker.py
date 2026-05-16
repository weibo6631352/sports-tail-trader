"""体育直播状态同步 worker。

该 worker 是 P2 维护任务：从外部比分源读取状态，匹配当前跟踪 market，并写入
EntryMetadataStore 供策略入场评估读取。它不直接判断交易机会，也不绕过风控下单。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import (
    LiveEvent,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
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

# audit dedupe LRU 容量：同一场比赛只在"状态字段"真实变化时落 audit_events，避免
# 5s 心跳级噪音淹没 audit 表（实测 sports_live_state_recorded 占 audit 99%+）。
# 8k 远超并发直播场数；触顶意味着异常多 event 突然同时直播或 hash 冲突。
_LIVE_STATE_AUDIT_DEDUPE_CAPACITY = 8_192

# recent_match_sources 环形缓冲容量：admin/校准 harness 拉最近 N 个匹配的源选择
# 信息（primary_source / contributing_sources / confidence / conflict_count）。
_RECENT_MATCH_SOURCES_CAPACITY = 256

SportsLiveSnapshotProvider = Callable[[], Awaitable[SportsLiveSnapshot]]
SportsLiveStateMatcher = Callable[
    [Market, tuple[LiveEvent, ...]],
    LiveStateMatch | None,
]

@runtime_checkable
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
    events_seen: int
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
        self._last_events_seen = 0
        self._last_markets_seen = 0
        self._last_matches = 0
        self._last_records_written = 0
        self._last_unmatched_markets = 0
        self._last_entry_signals_published = 0
        self._last_source_statuses: tuple[SportsLiveSourceStatus, ...] = ()
        self._last_events: tuple[LiveEvent, ...] = ()
        # 上轮各 source 的健康度，用于检测 evicted 状态转换；只在新 EVICTED 时
        # 发出 LIVE_STATE_SOURCE_EVICTED 一次，避免每轮重复刷生命周期。
        self._previous_source_health: dict[str, SportsLiveSourceHealth] = {}
        self._last_no_feasible_source_published: bool = False
        self._no_feasible_source: bool = False
        # audit dedupe：condition_id → 上次发布的 state-hash。仅在 hash 变化时
        # emit sports_live_state_recorded，让审计反映"真实状态变化"而不是 5s 心跳。
        self._last_audit_state_hash: OrderedDict[str, str] = OrderedDict()
        # 缺口 1 接线：近期每 market 实际匹配到的源选择信息（admin/校准 harness 读）。
        # 环形缓冲优先 FIFO，超容量自动丢弃最旧条目。
        self._recent_match_sources: deque[dict[str, Any]] = deque(
            maxlen=_RECENT_MATCH_SOURCES_CAPACITY
        )

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
            self._last_events_seen = result.events_seen
            self._last_markets_seen = result.markets_seen
            self._last_matches = result.matches
            self._last_records_written = result.records_written
            self._last_unmatched_markets = result.unmatched_markets
            self._last_entry_signals_published = result.entry_signals_published
            self._last_source_statuses = result.source_statuses
            return result
        finally:
            self._running = False

    def last_events(self) -> tuple[LiveEvent, ...]:
        """返回最近一次成功同步的直播事件集合，供高意图 discovery 只读使用。"""

        return self._last_events

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
            last_events_seen=self._last_events_seen,
            last_markets_seen=self._last_markets_seen,
            last_matches=self._last_matches,
            last_records_written=self._last_records_written,
            last_unmatched_markets=self._last_unmatched_markets,
            last_entry_signals_published=self._last_entry_signals_published,
            leagues=self._leagues,
            source_statuses=self._last_source_statuses,
        )

    def recent_match_sources(self, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        """返回最近匹配过的 market 对应的源选择信息只读快照。

        每条 dict 含 ``condition_id`` / ``primary_source`` / ``contributing_sources``
        / ``confidence`` / ``conflict_count``。admin runtime view 据此暴露
        per-market 源选择可观测性（缺口 1）。``limit`` 为 None 时返回全部缓冲条目。
        """

        items = list(self._recent_match_sources)
        if limit is not None and limit >= 0:
            items = items[-limit:]
        return tuple(items)

    async def _apply_snapshot(
        self,
        snapshot: SportsLiveSnapshot,
        *,
        started_at: datetime,
    ) -> SportsLiveSyncResult:
        markets = self._registry.snapshot().markets
        matches = await self._match_markets(markets, snapshot.events)
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
            self._record_match_sources(match)
            self._track_entry_signal_market(match=match)
            entry_signals += await self._publish_entry_signal_events(match=match)
            self._publish_live_state_lifecycle(match=match)
            # 异步落 audit_events——P3 优先级，失败不阻塞热路径；用于复盘"决策时
            # 的比分/时钟/赛况"这个目前的最大盲点。
            await self._publish_sports_live_state_recorded(snapshot=snapshot, match=match)

        completed_at = _utc_now()
        self._last_events = snapshot.events
        self._publish_source_health_lifecycle(snapshot.source_statuses, observed_at=snapshot.observed_at)
        return SportsLiveSyncResult(
            source=snapshot.source,
            started_at=started_at,
            completed_at=completed_at,
            events_seen=len(snapshot.events),
            markets_seen=len(markets),
            matches=len(matches),
            records_written=records_written,
            unmatched_markets=max(0, len(markets) - len(matches)),
            entry_signals_published=entry_signals,
            source_statuses=snapshot.source_statuses,
        )

    def _record_match_sources(self, match: LiveStateMatch) -> None:
        """缺口 1：每个匹配产生 per-market 源选择条目，供 admin / 校准 harness 复盘。"""

        event = match.event
        self._recent_match_sources.append(
            {
                "condition_id": match.market.condition_id,
                "market_slug": match.market.market_slug,
                "primary_source": match.primary_source or event.source,
                "contributing_sources": list(
                    match.contributing_sources or event.contributing_sources
                ),
                "confidence": float(match.confidence),
                "conflict_count": len(event.source_conflicts),
                "phase": match.phase,
                "signal_allowed": match.signal_allowed,
                "signal_reason": match.signal_reason,
            }
        )

    def _publish_source_health_lifecycle(
        self,
        statuses: tuple[SportsLiveSourceStatus, ...],
        *,
        observed_at: datetime,
    ) -> None:
        """发出 source eviction 与全源不可用的生命周期事件。"""

        if self._lifecycle_bus is None:
            self._no_feasible_source = self._compute_no_feasible_source(statuses)
            return
        for status in statuses:
            previous = self._previous_source_health.get(status.source)
            if status.health == SportsLiveSourceHealth.EVICTED and previous != SportsLiveSourceHealth.EVICTED:
                self._lifecycle_bus.publish(
                    LifecycleEvent.LIVE_STATE_SOURCE_EVICTED,
                    payload={
                        "source": status.source,
                        "last_error": status.last_error,
                        "consecutive_failures": status.consecutive_failures,
                        "cooldown_until": (
                            status.cooldown_until.isoformat() if status.cooldown_until is not None else None
                        ),
                    },
                )
            self._previous_source_health[status.source] = status.health
        no_feasible = self._compute_no_feasible_source(statuses)
        # 两端都要发：进入"全源不可用"时发 transition_in；离开时再发一次让订阅者
        # 能把缓存的暂停标志清掉。状态稳定（同样的真值）则不再重复刷。
        if no_feasible != self._last_no_feasible_source_published:
            self._lifecycle_bus.publish(
                LifecycleEvent.LIVE_STATE_NO_FEASIBLE_SOURCE,
                payload={
                    "observed_at": observed_at.isoformat(),
                    "source_statuses": [jsonable(status) for status in statuses],
                    "no_feasible_source": no_feasible,
                },
            )
        self._last_no_feasible_source_published = no_feasible
        self._no_feasible_source = no_feasible

    @staticmethod
    def _compute_no_feasible_source(statuses: tuple[SportsLiveSourceStatus, ...]) -> bool:
        if not statuses:
            return False
        return all(not status.success for status in statuses)

    @property
    def no_feasible_source(self) -> bool:
        """所有 source 都不健康时为 True，供策略 recovery 路径查询。"""

        return self._no_feasible_source

    async def _match_markets(
        self,
        markets: tuple[Market, ...],
        events: tuple[LiveEvent, ...],
    ) -> tuple[LiveStateMatch, ...]:
        results: list[LiveStateMatch] = []
        for index, market in enumerate(markets, start=1):
            match = self._match_live_state(market, events)
            if match is not None:
                results.append(match)
            if index % 10 == 0:
                # 单轮同步可能需要做 markets x events 的文本匹配；P2 任务必须让出事件循环。
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
                "source": match.event.source,
                "source_event_id": match.event.source_event_id,
                "signal_allowed": match.signal_allowed,
                "signal_reason": match.signal_reason,
                "phase": match.phase,
                "primary_source": match.primary_source,
                "contributing_sources": list(match.contributing_sources),
                "confidence": float(match.confidence),
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
        elif isinstance(tracker, SportsLiveMarketTracker):
            tracker.track_market(match.market)

    async def _publish_sports_live_state_recorded(
        self,
        *,
        snapshot: SportsLiveSnapshot,
        match: LiveStateMatch,
    ) -> None:
        if self._event_bus is None:
            return
        market = match.market
        # dedupe by "稳态字段 hash" — observed_at 每次都变（5s 心跳），不能算进 hash；
        # 只对 signal_allowed/signal_reason/phase + match.payload 内的 event-state
        # 字段（score/period/status 等）+ primary_source/conflict 摘要哈希。
        state_hash = _audit_state_hash(match)
        previous_hash = self._last_audit_state_hash.get(market.condition_id)
        if previous_hash == state_hash:
            self._last_audit_state_hash.move_to_end(market.condition_id)
            return
        self._last_audit_state_hash[market.condition_id] = state_hash
        self._last_audit_state_hash.move_to_end(market.condition_id)
        while len(self._last_audit_state_hash) > _LIVE_STATE_AUDIT_DEDUPE_CAPACITY:
            self._last_audit_state_hash.popitem(last=False)
        try:
            await self._event_bus.publish(
                OutboxPriority.P3,
                DomainEvent(
                    trace_id=f"sports-live-state-{uuid4().hex}",
                    event_type=DomainEventType.SPORTS_LIVE_STATE_RECORDED,
                    event_id=uuid4().hex,
                    market_slug=market.market_slug,
                    event_slug=market.event_slug,
                    condition_id=market.condition_id,
                    token_id=None,
                    reason=match.signal_reason or "",
                    payload={
                        "source": snapshot.source,
                        "observed_at": jsonable(snapshot.observed_at),
                        "signal_allowed": match.signal_allowed,
                        "signal_reason": match.signal_reason,
                        "phase": match.phase,
                        "primary_source": match.primary_source,
                        "contributing_sources": list(match.contributing_sources),
                        "confidence": float(match.confidence),
                        "source_conflicts": [
                            {
                                "field": c.field,
                                "winner_source": c.winner_source,
                                "winner_value": c.winner_value,
                                "loser_source": c.loser_source,
                                "loser_value": c.loser_value,
                                "decided_by": c.decided_by,
                            }
                            for c in match.event.source_conflicts
                        ],
                        "match_payload": jsonable(match.payload),
                    },
                ),
            )
        except Exception:
            # 体育事件落库纯属观测，失败不能反向阻塞 P2 worker。
            return

    async def _publish_entry_signal_events(self, *, match: LiveStateMatch) -> int:
        if not self._entry_signal_publish_enabled or self._event_bus is None:
            return 0
        if not match.signal_allowed:
            return 0
        market = match.market
        event = match.event
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
                        "source": event.source,
                        "source_event_id": event.source_event_id,
                        "primary_source": match.primary_source,
                        "contributing_sources": list(match.contributing_sources),
                        "confidence": float(match.confidence),
                        "match": jsonable(match.payload),
                        "signal_reason": match.signal_reason,
                    },
                ),
            )
            count += 1
        return count


def _audit_state_hash(match: LiveStateMatch) -> str:
    """对一次匹配的"稳态字段"产生哈希，用于 audit dedupe。

    observed_at / 5s 心跳无关字段（如 raw_status 文本时间戳）刻意排除——它们每次
    都变，不能作为去重 key。signal_allowed / signal_reason / phase / primary_source
    / contributing_sources / confidence / conflict_count 都参与 hash，因为这些
    是"非心跳变化"——融合主源切换、源信任度变化、冲突变化都要落 audit。
    event-state 字段（home_score/away_score/period/status/seconds_remaining 等）
    通过 match.payload["live_game"] 透传，整体序列化为 JSON 后哈希足够稳定。
    """

    live_game = (match.payload.get("live_game") if isinstance(match.payload, dict) else None) or {}
    # 拷贝并裁掉每次都变的时间字段，让 hash 反映"非心跳变化"。
    if isinstance(live_game, dict):
        live_game_for_hash = {k: v for k, v in live_game.items() if k != "observed_at"}
    else:
        live_game_for_hash = live_game
    key_payload = {
        "signal_allowed": match.signal_allowed,
        "signal_reason": match.signal_reason,
        "phase": match.phase,
        "primary_source": match.primary_source,
        "contributing_sources": list(match.contributing_sources),
        "confidence_bucket": round(float(match.confidence), 2),
        "conflict_count": len(match.event.source_conflicts),
        "event": live_game_for_hash,
    }
    blob = json.dumps(key_payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
