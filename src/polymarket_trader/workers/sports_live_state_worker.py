"""体育直播状态同步 worker。

该 worker 是 P2 维护任务：从外部比分源读取状态，匹配当前跟踪 market，并写入
EntryMetadataStore 供策略入场评估读取。它不直接判断交易机会，也不绕过风控下单。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import (
    LiveEvent,
    SportsLiveGameStatus,
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


@runtime_checkable
class SportsLiveMarketPauser(Protocol):
    """比赛进入终态（ended/cancelled/retired）时通知 account_state 暂停该市场。"""

    def pause_market(self, condition_id: str, *, reason: str, **kwargs: Any) -> Any: ...


# 仅 CANCELLED / RETIRED 立即 pause——这两类比赛不会再结算赢方，市场也无套利价值。
# ENDED 故意 *不* 在这里 pause：策略侧 entry_signal_gate 把 "ended_not_closed" 视作
# 扫尾入场信号（§17：已分出胜负、等 Polymarket 结算的赢方挂 BUY 锁定确定性收益），
# 自动 pause 会误杀此类套利窗口。ENDED → 死盘的 prune 仍由 end_date+6h grace 兜底。
_TERMINAL_STATUS_PAUSE_REASONS: dict[str, str] = {
    SportsLiveGameStatus.CANCELLED.value: "sports_live_state_cancelled",
    SportsLiveGameStatus.RETIRED.value: "sports_live_state_retired",
}


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
        market_pauser: SportsLiveMarketPauser | None = None,
        enabled: bool = True,
        source: str = "espn",
        leagues: tuple[str, ...] = (),
        publish_entry_signals: bool = True,
        audit_min_interval_s: float = 0.0,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._match_live_state = match_live_state
        self._registry = registry
        self._entry_metadata_store = entry_metadata_store
        self._event_bus = event_bus
        self._market_tracker = market_tracker
        self._lifecycle_bus = lifecycle_bus
        self._market_pauser = market_pauser
        # 已对外发出 terminal pause 的 condition_id 集合——避免每秒重复 pause；
        # 比赛进入 ENDED/CANCELLED/RETIRED 后不会回到 LIVE，不需要 revert 逻辑。
        # registry 退订该 market 后下一轮 _match_markets 已经看不到它，集合长期上限。
        # _terminal_status_paused 已迁移到 registry.companion(cid).terminal_pause_emitted
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
        # 增量匹配缓存：condition_id → source_event_id（稳定匹配后直接按 ID 查 event，
        # 不再每轮全量文本扫描）。hook 仍每轮调用（signal_allowed 依赖盘口价格等
        # 非 event 字段，不能按 event 指纹跳过）；audit 去重由 _last_audit_state_hash 承担。
        self._match_cache: dict[str, str] = {}
        # 上轮各 source 的健康度，用于检测 evicted 状态转换；只在新 EVICTED 时
        # 发出 LIVE_STATE_SOURCE_EVICTED 一次，避免每轮重复刷生命周期。
        self._previous_source_health: dict[str, SportsLiveSourceHealth] = {}
        self._last_no_feasible_source_published: bool = False
        self._no_feasible_source: bool = False
        # audit dedupe + 30s 节流全部挂 registry.companion(cid):market prune
        # 时 companion 自动消失,无需自己 dict + cap.
        self._audit_min_interval_s: float = audit_min_interval_s
        # 缺口 1 接线：近期每 market 实际匹配到的源选择信息（admin/校准 harness 读）。
        # 环形缓冲优先 FIFO，超容量自动丢弃最旧条目。
        self._recent_match_sources: deque[dict[str, Any]] = deque(
            maxlen=_RECENT_MATCH_SOURCES_CAPACITY
        )
        # 匹配失败 gap 去重：只在 market 从"有匹配"→"无匹配"或首次发现时落 audit。
        # condition_id → 上次 gap 记录的 minute bucket（每分钟最多记一次）。
        # _last_gap_recorded_minute 已迁移到 registry.companion(cid).last_gap_recorded_minute

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
            self._maybe_pause_terminal_market(match)
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
            # WS 订阅决策不再写 market 上的 flag——ws_loops.should_subscribe_ws
            # 实时读 entry_metadata（上一行 upsert 写入的）+ trading_status 做单一
            # gate。这样 phase=ended 自然让下次订阅刷新时 gate 失败 → 取消订阅，
            # 无需 worker 主动清理任何标志位。
            self._record_match_sources(match)
            self._track_entry_signal_market(match=match)
            entry_signals += await self._publish_entry_signal_events(match=match)
            self._publish_live_state_lifecycle(match=match)
            # 异步落 audit_events——P3 优先级，失败不阻塞热路径；用于复盘"决策时
            # 的比分/时钟/赛况"这个目前的最大盲点。
            await self._publish_sports_live_state_recorded(snapshot=snapshot, match=match)

        # Include fast-path cached matches (not in `matches` because their event
        # state did not change this round) so they are not mistakenly reported as gaps.
        matched_condition_ids = {m.market.condition_id for m in matches} | set(self._match_cache)
        now = _utc_now()
        now_minute = int(now.timestamp()) // 60
        for market in markets:
            companion = self._registry.companion(market.condition_id)
            if market.condition_id in matched_condition_ids:
                # 清除 gap 记录——已匹配上，下次失配时重新记录。
                if companion is not None:
                    companion.last_gap_recorded_minute = None
                continue
            # 本轮没匹配到（比赛结束 / Goalserve 不再返回 / event_slug 改名）。
            # 不再写任何 flag——entry_metadata 自然衰老（worker 不更新 phase），
            # ws_loops 下次刷新时 gate 失败自动取消订阅。
            # 只对 game_start_time 已过去的市场（比赛应该正在进行）记录 gap。
            if market.game_start_time is None or market.game_start_time > now:
                continue
            if companion is None:
                continue  # cid 已 prune,跳过
            last_minute = companion.last_gap_recorded_minute
            if last_minute is not None and now_minute - last_minute < 5:
                continue
            companion.last_gap_recorded_minute = now_minute
            await self._publish_live_match_gap(market=market, snapshot=snapshot, now=now)

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

    def _maybe_pause_terminal_market(self, match: LiveStateMatch) -> None:
        """比赛进入 ENDED/CANCELLED/RETIRED 时通知 account_state pause 该市场.

        这是**事实信号**(比赛客观结束),不是策略决策,应保留 pause:
        - account_state.market_pauses 写入 reason=sports_live_state_ended/cancelled/retired
        - reconcile 看到 TERMINAL_LIVE_STATE_PAUSE_REASONS → 无敞口 market 立即 prune
        - reconcile 自己的 RECONCILE source pause 已删(避免瞬态闪烁),与此互不影响.
        """
        if self._market_pauser is None:
            return
        condition_id = match.market.condition_id
        companion = self._registry.companion(condition_id) if self._registry else None
        if companion is None or companion.terminal_pause_emitted:
            return
        reason = _TERMINAL_STATUS_PAUSE_REASONS.get(match.event.status.value)
        if reason is None:
            return
        try:
            self._market_pauser.pause_market(
                condition_id,
                reason=reason,
                source="sports_live_state_worker",
            )
        except Exception:
            return
        companion.terminal_pause_emitted = True

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
        # O(N) index built once per sync; cached-market lookups are O(1) with no text scan.
        events_by_id: dict[str, LiveEvent] = {e.source_event_id: e for e in events}

        # Evict cache entries for markets no longer tracked to bound memory growth.
        active_ids = {m.condition_id for m in markets}
        for stale in set(self._match_cache) - active_ids:
            del self._match_cache[stale]

        results: list[LiveStateMatch] = []
        needs_text_match: list[Market] = []
        processed = 0

        for market in markets:
            cid = market.condition_id
            cached_event_id = self._match_cache.get(cid)

            if cached_event_id is not None:
                event = events_by_id.get(cached_event_id)
                if event is None:
                    # Live event evicted (game ended / orphan TTL) — drop cache, re-search.
                    del self._match_cache[cid]
                    needs_text_match.append(market)
                else:
                    # Fast path: known event, no text search. Hook still runs every round
                    # because signal_allowed depends on orderbook price, not just event state.
                    match = self._match_live_state(market, (event,))
                    if match is not None:
                        results.append(match)
                    else:
                        # Hook rejected the cached event (e.g. game moved to ENDED).
                        del self._match_cache[cid]
                        needs_text_match.append(market)
            else:
                needs_text_match.append(market)

            processed += 1
            if processed % 10 == 0:
                await asyncio.sleep(0)

        # Slow path: game-level text matching for unmatched/evicted markets.
        # Group by event_slug so one text search serves all markets in the same game.
        event_groups: dict[str, list[Market]] = {}
        no_slug: list[Market] = []
        for market in needs_text_match:
            slug = market.event_slug
            if slug:
                event_groups.setdefault(slug, []).append(market)
            else:
                no_slug.append(market)

        for group in event_groups.values():
            first = self._match_live_state(group[0], events)
            if first is not None:
                results.append(first)
                self._match_cache[group[0].condition_id] = first.event.source_event_id
            processed += 1
            if processed % 10 == 0:
                await asyncio.sleep(0)
            for market in group[1:]:
                if first is not None:
                    m = self._match_live_state(market, (first.event,))
                    if m is not None:
                        results.append(m)
                        self._match_cache[market.condition_id] = first.event.source_event_id
                processed += 1
                if processed % 10 == 0:
                    await asyncio.sleep(0)

        for market in no_slug:
            match = self._match_live_state(market, events)
            if match is not None:
                results.append(match)
                self._match_cache[market.condition_id] = match.event.source_event_id
            processed += 1
            if processed % 10 == 0:
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
        companion = self._registry.companion(market.condition_id) if self._registry else None
        if companion is None:
            return  # cid 已 prune,不应再发 audit
        if companion.last_audit_state_hash == state_hash:
            return
        # 30s 最小间隔兜底:state_hash 含嵌套 sub-state 的时间字段,5s 心跳每次都变,
        # 单纯 hash dedupe 失效.强制 30s 颗粒度,稳态 audit 写入 < 7/sec.
        import time as _time
        now_mono = _time.monotonic()
        if (now_mono - companion.last_audit_emit_at_mono) < self._audit_min_interval_s:
            return
        companion.last_audit_emit_at_mono = now_mono
        companion.last_audit_state_hash = state_hash
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

    async def _publish_live_match_gap(
        self,
        *,
        market: Market,
        snapshot: SportsLiveSnapshot,
        now: datetime,
    ) -> None:
        """为"应该有直播数据但匹配失败"的市场落 audit 事件，便于追溯和排查。"""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                OutboxPriority.P3,
                DomainEvent(
                    trace_id=f"sports-live-gap-{uuid4().hex}",
                    event_type=DomainEventType.SPORTS_LIVE_MATCH_GAP_RECORDED,
                    event_id=uuid4().hex,
                    market_slug=market.market_slug,
                    event_slug=market.event_slug,
                    condition_id=market.condition_id,
                    token_id=None,
                    reason="missing_live_game_state",
                    payload={
                        "source": snapshot.source,
                        "observed_at": jsonable(now),
                        "game_start_time": jsonable(market.game_start_time),
                        "events_seen": len(snapshot.events),
                        "gap_urgency": "started_or_past_due",
                    },
                ),
            )
        except Exception:
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
                    # 与 ORDERBOOK_SNAPSHOT_UPDATED 共用同一 merge_key，使信号在满队列中
                    # 复用已有 slot 而非等待新槽位，避免被 WS 洪流阻塞。
                    merge_key=f"orderbook_snapshot_updated|{token_id}",
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
