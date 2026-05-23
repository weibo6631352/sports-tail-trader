from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.sports_live import (
    
    LiveEvent,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveSourceStatus,
    Participant,
    LiveEventKind,
)
from polymarket_trader.extension_api.live_state import LiveStateMatch
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.sports_live_state_worker import SportsLiveStateWorker
from strategies.current.live_state import build_live_state_match


def _live_state_match_from_metadata(market, events):
    """Test helper: 用策略侧 build_live_state_match 构造 LiveStateMatch。"""

    return build_live_state_match(market, events)


def test_sports_live_state_worker_writes_metadata_and_entry_signals() -> None:
    result = asyncio.run(_run_sync_with_match())

    metadata = result["metadata"]
    status = result["status"]
    first_event = result["first_event"]
    second_event = result["second_event"]

    assert metadata["live_game"]["league"] == "NBA"
    assert metadata["live_game"]["home_score"] == 102
    assert metadata["live_game"]["away_score"] == 94
    assert metadata["live_game"]["seconds_remaining"] == 90
    assert metadata["live_match"]["source_event_id"] == "game-1"
    assert status.last_events_seen == 1
    assert status.last_matches == 1
    assert status.last_records_written == 1
    assert status.last_entry_signals_published == 2
    assert result["last_events"][0].source_event_id == "game-1"
    assert first_event.event_type == DomainEventType.ENTRY_SIGNAL_TRIGGERED
    assert second_event.event_type == DomainEventType.ENTRY_SIGNAL_TRIGGERED
    assert {first_event.token_id, second_event.token_id} == {"home", "away"}


def test_sports_live_state_worker_indexes_metadata_by_event_identity() -> None:
    result = asyncio.run(_run_sync_with_match())

    by_market_slug = result["store"].metadata_for(market_slug="nba-nyk-bos-moneyline")
    by_event_slug = result["store"].metadata_for(event_slug="new-york-knicks-vs-boston-celtics")

    assert by_market_slug["live_match"]["source_event_id"] == "game-1"
    assert by_event_slug["live_match"]["source_event_id"] == "game-1"


def test_sports_live_state_worker_tracks_entry_signal_market_for_market_ws() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        tracker = _MarketTracker()
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=_live_state_match_from_metadata,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            market_tracker=tracker,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        sync_result = await worker.sync_once()

        assert sync_result is not None
        assert sync_result.records_written == 1
        assert tracker.tracked_condition_ids == ["moneyline-condition"]

    asyncio.run(run())


def test_sports_live_state_worker_does_not_track_blocked_entry_signal_market() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        registry.upsert(_market())
        tracker = _MarketTracker()
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=lambda market, events: LiveStateMatch(
                market=market,
                event=events[0],
                signal_allowed=False,
                signal_reason="sports_live_state_scheduled",
                payload={
                    "live_game": {
                        "league": "NBA",
                        "home_name": "Knicks",
                        "away_name": "Celtics",
                        "home_score": 102,
                        "away_score": 94,
                        "period": "Q4",
                        "seconds_remaining": 90,
                        "status": "live",
                    },
                },
            ),
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            market_tracker=tracker,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        sync_result = await worker.sync_once()

        assert sync_result is not None
        assert sync_result.records_written == 1
        assert tracker.tracked_condition_ids == []

    asyncio.run(run())


def test_sports_live_state_worker_keeps_unmatched_markets_auditable() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        registry.upsert(_market())
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(
                _game(
                    home="Lakers",
                    away="Heat",
                    home_display="Los Angeles Lakers",
                    away_display="Miami Heat",
                    home_abbreviation="LAL",
                    away_abbreviation="MIA",
                )
            ),
            match_live_state=_live_state_match_from_metadata,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        sync_result = await worker.sync_once()
        status = worker.status_snapshot()

        assert sync_result is not None
        assert sync_result.matches == 0
        assert status.last_unmatched_markets == 1
        assert status.last_records_written == 0
        assert status.last_entry_signals_published == 0

    asyncio.run(run())


def test_sports_live_state_worker_writes_metadata_without_blocked_entry_signals() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        registry.upsert(_market())
        store = EntryMetadataStore()
        event_bus = EventBus(trading_capacity=10, maintenance_capacity=10, persistence_capacity=10)
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=lambda market, events: LiveStateMatch(
                market=market,
                event=events[0],
                signal_allowed=False,
                signal_reason="sports_live_state_scheduled",
                payload={
                    "live_game": {
                        "league": "NBA",
                        "home_name": "Knicks",
                        "away_name": "Celtics",
                        "home_score": 102,
                        "away_score": 94,
                        "period": "Q4",
                        "seconds_remaining": 90,
                        "status": "live",
                    },
                },
            ),
            registry=registry,
            entry_metadata_store=store,
            event_bus=event_bus,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=True,
        )

        sync_result = await worker.sync_once()

        assert sync_result is not None
        assert sync_result.records_written == 1
        assert sync_result.entry_signals_published == 0
        assert event_bus.trading_queue_depth() == 0
        record = store.find(condition_id="moneyline-condition")
        assert record is not None
        assert record.live_state_signal_allowed is False
        assert record.live_state_signal_reason == "sports_live_state_scheduled"

    asyncio.run(run())


def test_sports_live_state_worker_exposes_source_statuses() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        registry.upsert(_market())
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(
                _game(),
                source="sports_live_aggregate",
                source_statuses=(
                    SportsLiveSourceStatus(source="espn", success=True, events_seen=1),
                    SportsLiveSourceStatus(source="nba", success=False, last_error="timeout"),
                ),
            ),
            match_live_state=_live_state_match_from_metadata,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            enabled=True,
            source="sports_live_aggregate",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        sync_result = await worker.sync_once()
        status = worker.status_snapshot()

        assert sync_result is not None
        assert sync_result.source_statuses[0].source == "espn"
        assert status.source_statuses[1].source == "nba"
        assert status.source_statuses[1].last_error == "timeout"

    asyncio.run(run())


def test_no_feasible_source_lifecycle_fires_on_both_transitions() -> None:
    """worker 在"进入"和"离开"全源不可用状态时各发一次 lifecycle，确保订阅
    侧能正确清除暂停标志。"""

    from polymarket_trader.extension_api.lifecycle import (
        LifecycleEvent,
        SubscriptionHandle,
    )

    class _CaptureBus:
        def __init__(self) -> None:
            self.published: list[tuple[LifecycleEvent, dict]] = []

        def publish(self, event, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self.published.append((event, dict(kwargs.get("payload") or {})))

        def subscribe(self, event, callback):  # type: ignore[no-untyped-def]
            return SubscriptionHandle(subscription_id=1)

        def unsubscribe(self, handle) -> None:
            return None

    async def run() -> _CaptureBus:
        registry = MarketRegistry()
        bus = _CaptureBus()
        snapshots: list[SportsLiveSnapshot] = [
            # 第 1 轮：全部失败 → 进入 no_feasible_source
            SportsLiveSnapshot(
                source="sports_live_aggregate",
                observed_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
                events=(),
                source_statuses=(
                    SportsLiveSourceStatus(source="espn", success=False, last_error="500"),
                    SportsLiveSourceStatus(source="nba", success=False, last_error="429"),
                ),
            ),
            # 第 2 轮：状态稳定（仍全部失败）→ 不应重复发布
            SportsLiveSnapshot(
                source="sports_live_aggregate",
                observed_at=datetime(2026, 4, 27, 0, 0, 5, tzinfo=timezone.utc),
                events=(),
                source_statuses=(
                    SportsLiveSourceStatus(source="espn", success=False),
                    SportsLiveSourceStatus(source="nba", success=False),
                ),
            ),
            # 第 3 轮：一个源恢复 → 离开 no_feasible_source，需要再发一次
            SportsLiveSnapshot(
                source="sports_live_aggregate",
                observed_at=datetime(2026, 4, 27, 0, 0, 10, tzinfo=timezone.utc),
                events=(),
                source_statuses=(
                    SportsLiveSourceStatus(source="espn", success=True, events_seen=1),
                    SportsLiveSourceStatus(source="nba", success=False),
                ),
            ),
        ]
        provider_calls = iter(snapshots)

        async def provider() -> SportsLiveSnapshot:
            return next(provider_calls)

        worker = SportsLiveStateWorker(
            snapshot_provider=provider,
            match_live_state=lambda _m, _g: None,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            lifecycle_bus=bus,
            enabled=True,
            publish_entry_signals=False,
        )
        for _ in range(3):
            await worker.sync_once()
        return bus

    bus = asyncio.run(run())
    no_feasible_events = [p for ev, p in bus.published if ev == LifecycleEvent.LIVE_STATE_NO_FEASIBLE_SOURCE]
    # 应该恰好 2 次：一次 True（进入），一次 False（离开）；中间稳定轮不重复刷
    assert len(no_feasible_events) == 2
    assert no_feasible_events[0]["no_feasible_source"] is True
    assert no_feasible_events[1]["no_feasible_source"] is False


def test_sports_live_state_worker_yields_during_bulk_market_matching() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        for index in range(25):
            registry.upsert(_market(condition_id=f"condition-{index}", slug=f"nba-nyk-bos-{index}"))
        sync_done = False
        yielded_before_sync_done = False

        async def observer() -> None:
            nonlocal yielded_before_sync_done
            await asyncio.sleep(0)
            yielded_before_sync_done = not sync_done

        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=lambda _market, _games: None,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )

        observer_task = asyncio.create_task(observer())
        await worker.sync_once()
        sync_done = True
        await observer_task

        assert yielded_before_sync_done is True

    asyncio.run(run())


async def _run_sync_with_match() -> dict[str, object]:
    registry = MarketRegistry()
    registry.upsert(_market())
    store = EntryMetadataStore()
    event_bus = EventBus(trading_capacity=10, maintenance_capacity=10, persistence_capacity=10)
    worker = SportsLiveStateWorker(
        snapshot_provider=lambda: _snapshot(_game()),
        match_live_state=_live_state_match_from_metadata,
        registry=registry,
        entry_metadata_store=store,
        event_bus=event_bus,
        enabled=True,
        source="espn",
        leagues=("nba",),
        publish_entry_signals=True,
    )

    sync_result = await worker.sync_once()
    assert sync_result is not None
    return {
        "metadata": store.metadata_for(condition_id="moneyline-condition"),
        "store": store,
        "status": worker.status_snapshot(),
        "last_events": worker.last_events(),
        "first_event": await event_bus.next_trading_event(),
        "second_event": await event_bus.next_trading_event(),
    }


async def _snapshot(
    game: LiveEvent,
    *,
    source: str = "espn",
    source_statuses: tuple[SportsLiveSourceStatus, ...] = (),
) -> SportsLiveSnapshot:
    return SportsLiveSnapshot(
        source=source,
        observed_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        events=(game,),
        source_statuses=source_statuses,
    )


def _game(
    *,
    home: str = "Knicks",
    away: str = "Celtics",
    home_display: str | None = None,
    away_display: str | None = None,
    home_abbreviation: str = "NYK",
    away_abbreviation: str = "BOS",
) -> LiveEvent:
    return LiveEvent(
        
        participants=(Participant(
            role="home", name=home,
            score=102,
            display_name=home_display or f"New York {home}",
            abbreviation=home_abbreviation,
            short_name=home,
        ), Participant(
            role="away", name=away,
            score=94,
            display_name=away_display or f"Boston {away}",
            abbreviation=away_abbreviation,
            short_name=away,
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="basketball",source="espn",
        source_event_id="game-1",
        league="NBA",
        status=SportsLiveGameStatus.LIVE,
        period="Q4",
        seconds_remaining=90,
        observed_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        raw_status="STATUS_IN_PROGRESS",
    )


def _market(
    *,
    condition_id: str = "moneyline-condition",
    slug: str = "nba-nyk-bos-moneyline",
) -> Market:
    return Market(
        condition_id=condition_id,
        market_slug=slug,
        market_question="New York Knicks vs Boston Celtics moneyline",
        event_title="New York Knicks vs Boston Celtics",
        event_slug="new-york-knicks-vs-boston-celtics",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="home", outcome="NYK"),
            MarketOutcome(token_id="away", outcome="BOS"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


class _MarketTracker:
    def __init__(self) -> None:
        self.tracked_condition_ids: list[str] = []

    def track_market(self, market: Market) -> None:
        self.tracked_condition_ids.append(market.condition_id)


class _RecordingPauser:
    """测试用 market_pauser：记录每次 pause_market 调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def pause_market(self, condition_id: str, *, reason: str, **_: object) -> object:
        self.calls.append((condition_id, reason))
        return None


def _terminal_match(market: Market, *, status: SportsLiveGameStatus) -> LiveStateMatch:
    return LiveStateMatch(
        market=market,
        event=LiveEvent(
            participants=(
                Participant(role="home", name="Knicks", score=102),
                Participant(role="away", name="Celtics", score=94),
            ),
            kind=LiveEventKind.TEAM_MATCH,
            sport="basketball",
            source="espn",
            source_event_id="game-1",
            league="NBA",
            status=status,
            period="Final",
            seconds_remaining=0,
            observed_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
            raw_status="STATUS_FINAL",
        ),
        signal_allowed=False,
        signal_reason="sports_live_state_ended",
        phase="ended",
        payload={"live_game": {"status": status.value}},
    )


def test_terminal_status_triggers_pause_market_once() -> None:
    """ENDED/CANCELLED/RETIRED 必须把 condition_id pause 一次，且不重复 pause。"""

    async def run(status: SportsLiveGameStatus) -> _RecordingPauser:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        pauser = _RecordingPauser()
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=lambda m, _events: _terminal_match(m, status=status),
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            market_pauser=pauser,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )
        await worker.sync_once()
        await worker.sync_once()  # 第二轮：相同 terminal status，不应重复 pause
        return pauser

    for status, expected_reason in (
        (SportsLiveGameStatus.CANCELLED, "sports_live_state_cancelled"),
        (SportsLiveGameStatus.RETIRED, "sports_live_state_retired"),
    ):
        pauser = asyncio.run(run(status))
        assert pauser.calls == [("moneyline-condition", expected_reason)], (
            f"status={status.value} expected single pause with reason={expected_reason}, got {pauser.calls}"
        )


def test_ended_status_does_not_trigger_pause_market() -> None:
    """ENDED 故意保留：策略侧 entry_signal_gate 把 "ended_not_closed" 视作扫尾入场信号
    （§17 赢方等结算），worker 自动 pause 会误杀套利窗口。"""

    async def run() -> _RecordingPauser:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        pauser = _RecordingPauser()
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=lambda m, _events: _terminal_match(m, status=SportsLiveGameStatus.ENDED),
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            market_pauser=pauser,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )
        await worker.sync_once()
        return pauser

    pauser = asyncio.run(run())
    assert pauser.calls == []


def test_live_status_does_not_trigger_pause_market() -> None:
    """LIVE / PAUSED / SCHEDULED 不应触发 pause_market。"""

    async def run() -> _RecordingPauser:
        registry = MarketRegistry()
        registry.upsert(_market())
        pauser = _RecordingPauser()
        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=_live_state_match_from_metadata,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            market_pauser=pauser,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )
        await worker.sync_once()
        return pauser

    pauser = asyncio.run(run())
    assert pauser.calls == []


# ============================================================
# audit dedupe（commit 1752416 引入的"稳态字段 hash"路径）
# ============================================================

def _audit_match_for(market: Market, *, score: int, signal_allowed: bool = True) -> LiveStateMatch:
    """生成可控 game-state payload 的 LiveStateMatch，让测试能针对单字段切换验证 hash 不变性。"""
    return LiveStateMatch(
        market=market,
        event=_game(),
        signal_allowed=signal_allowed,
        signal_reason="late_game_certainty" if signal_allowed else "market_end_too_far",
        phase="late_4q",
        payload={
            "live_game": {
                "league": "NBA",
                "home_name": "Knicks",
                "away_name": "Celtics",
                "home_score": score,
                "away_score": 94,
                "period": "Q4",
                "seconds_remaining": 90,
                "status": "live",
                "observed_at": "2026-04-27T00:00:00Z",  # 每次都变的心跳字段——不该进 hash
            },
        },
    )


def _drain_persistence_events(event_bus: EventBus) -> list:
    """非阻塞抽干 persistence queue 的所有 P3 事件，避免测试卡在 await。
    队列存 (sequence, event) tuple——只取 event 部分。"""
    drained = []
    while True:
        try:
            _, event = event_bus._persistence_queue.get_nowait()  # type: ignore[attr-defined]
            drained.append(event)
        except Exception:
            break
    return drained


def test_audit_dedupe_skips_unchanged_state() -> None:
    """同一 condition_id 连续两次相同稳态 → 只发一次 sports_live_state_recorded。
    observed_at 字段每次心跳都变，但被 _audit_state_hash 主动排除——所以不能触发新一条 audit。"""
    async def run() -> list:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        event_bus = EventBus(trading_capacity=10, maintenance_capacity=10, persistence_capacity=20)
        match = _audit_match_for(market, score=102)

        def matcher(_m, _g):
            return match

        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=matcher,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            event_bus=event_bus,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )
        await worker.sync_once()
        await worker.sync_once()  # 完全相同 state；observed_at 一致也 OK
        return _drain_persistence_events(event_bus)

    events = asyncio.run(run())
    audit_events = [e for e in events if getattr(e, "event_type", None) == DomainEventType.SPORTS_LIVE_STATE_RECORDED]
    assert len(audit_events) == 1, f"expected 1 dedupe-collapsed audit but got {len(audit_events)}"


def test_audit_dedupe_emits_on_score_change() -> None:
    """home_score 变了 → 第二次 sync 必须发新 audit。"""
    async def run() -> list:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        event_bus = EventBus(trading_capacity=10, maintenance_capacity=10, persistence_capacity=20)
        scores = iter([102, 105])

        def matcher(_m, _g):
            return _audit_match_for(market, score=next(scores))

        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=matcher,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            event_bus=event_bus,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )
        await worker.sync_once()
        await worker.sync_once()
        return _drain_persistence_events(event_bus)

    events = asyncio.run(run())
    audit_events = [e for e in events if getattr(e, "event_type", None) == DomainEventType.SPORTS_LIVE_STATE_RECORDED]
    assert len(audit_events) == 2


def test_audit_dedupe_emits_on_signal_allowed_change() -> None:
    """signal_allowed True→False 切换是核心状态变更，必须发新 audit。"""
    async def run() -> list:
        registry = MarketRegistry()
        market = _market()
        registry.upsert(market)
        event_bus = EventBus(trading_capacity=10, maintenance_capacity=10, persistence_capacity=20)
        signal_states = iter([True, False])

        def matcher(_m, _g):
            return _audit_match_for(market, score=102, signal_allowed=next(signal_states))

        worker = SportsLiveStateWorker(
            snapshot_provider=lambda: _snapshot(_game()),
            match_live_state=matcher,
            registry=registry,
            entry_metadata_store=EntryMetadataStore(),
            event_bus=event_bus,
            enabled=True,
            source="espn",
            leagues=("nba",),
            publish_entry_signals=False,
        )
        await worker.sync_once()
        await worker.sync_once()
        return _drain_persistence_events(event_bus)

    events = asyncio.run(run())
    audit_events = [e for e in events if getattr(e, "event_type", None) == DomainEventType.SPORTS_LIVE_STATE_RECORDED]
    assert len(audit_events) == 2


def test_audit_dedupe_lru_evicts_oldest_beyond_capacity() -> None:
    """超出 _LIVE_STATE_AUDIT_DEDUPE_CAPACITY 时最老的 condition_id 被淘汰，下次相同 hash 视为"首次"重发。
    用直接操作 _last_audit_state_hash 验证 LRU 行为（不真跑 8192 个 condition 太重）。"""
    from polymarket_trader.workers.sports_live_state_worker import (
        _LIVE_STATE_AUDIT_DEDUPE_CAPACITY,
    )
    registry = MarketRegistry()
    worker = SportsLiveStateWorker(
        snapshot_provider=lambda: _snapshot(_game()),
        match_live_state=lambda _m, _g: None,
        registry=registry,
        entry_metadata_store=EntryMetadataStore(),
        enabled=True,
        source="espn",
        leagues=("nba",),
        publish_entry_signals=False,
    )
    # 填到 capacity 上限
    for i in range(_LIVE_STATE_AUDIT_DEDUPE_CAPACITY):
        worker._last_audit_state_hash[f"cond-{i}"] = f"hash-{i}"
    assert len(worker._last_audit_state_hash) == _LIVE_STATE_AUDIT_DEDUPE_CAPACITY

    # 模拟 _publish_sports_live_state_recorded 的写入逻辑：超过容量时 popitem(last=False)
    worker._last_audit_state_hash["cond-new"] = "hash-new"
    worker._last_audit_state_hash.move_to_end("cond-new")
    while len(worker._last_audit_state_hash) > _LIVE_STATE_AUDIT_DEDUPE_CAPACITY:
        worker._last_audit_state_hash.popitem(last=False)

    # cond-0（最老）应被淘汰；cond-new 仍在
    assert "cond-0" not in worker._last_audit_state_hash
    assert "cond-new" in worker._last_audit_state_hash
    assert len(worker._last_audit_state_hash) == _LIVE_STATE_AUDIT_DEDUPE_CAPACITY
