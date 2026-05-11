"""PersistenceWorker P0 契约测试。

PersistenceWorker 是 CLAUDE.md §7 强调的"outbox → DB 异步承接 P0 副作用"的关键消费者：
- 持久化失败不得反向阻塞交易热路径
- 失败必须 retry / dead_letter，不能静默丢
- 低优先级事件可合并，关键事件不合并

此前 worker.py 597 行没有任何直接测试。本文件覆盖：
1. run_once 在 outbox 空时返回 None
2. 正常 audit 事件 → repository.save_audit_event(s) + outbox.ack
3. order 事件 → 同时落 order + audit + outbox 表
4. repository 抛 retryable 错误 + 未达 max_retry → outbox.retry
5. repository 抛错且达到 max_retry → outbox.dead_letter
6. 低优先级同 route_key 多事件 → 合并（仅保留最后一条）
7. 关键事件（priority<=1）永不合并
8. snapshot 反映累积统计
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

import pytest

from polymarket_trader.domain.events import DomainEventType, OutboxEvent
from polymarket_trader.workers.persistence.worker import PersistenceWorker


class _StubOutbox:
    """实现 PersistenceOutbox Protocol；用 list 模拟队列。
    get() 阻塞直到有事件或超时（等同真实 outbox 的 asyncio.Queue 行为）。"""

    def __init__(self, events: Sequence[OutboxEvent] | None = None) -> None:
        self._queue: list[OutboxEvent] = list(events or [])
        self.acked: list[OutboxEvent] = []
        self.retried: list[tuple[OutboxEvent, str | None]] = []
        self.dead_lettered: list[tuple[OutboxEvent, str | None]] = []

    def push(self, event: OutboxEvent) -> None:
        self._queue.append(event)

    async def get(self) -> OutboxEvent:
        # 模拟真实 outbox：空队列时 await 永不返回，让 worker 的 wait_for 超时。
        while not self._queue:
            await asyncio.sleep(0.001)
        return self._queue.pop(0)

    async def ack(self, event: str | OutboxEvent) -> None:
        if isinstance(event, OutboxEvent):
            self.acked.append(event)

    async def retry(
        self, event: str | OutboxEvent, *, last_error: str | None = None
    ) -> OutboxEvent:
        assert isinstance(event, OutboxEvent)
        self.retried.append((event, last_error))
        return event

    async def dead_letter(
        self, event: str | OutboxEvent, *, last_error: str | None = None
    ) -> OutboxEvent:
        assert isinstance(event, OutboxEvent)
        self.dead_lettered.append((event, last_error))
        return event


class _StubRepository:
    """实现 PersistenceRepository Protocol；记录 save_* 调用 + 可注入失败。"""

    def __init__(self, *, fail_on_kinds: set[str] | None = None, fail_exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_on_kinds = fail_on_kinds or set()
        self.fail_exc = fail_exc or RuntimeError("simulated_db_failure")

    async def _record(self, kind: str, payload: Any) -> None:
        self.calls.append((kind, payload))
        if kind in self.fail_on_kinds:
            raise self.fail_exc

    # 所有 save_* / save_*s 都路由到 _record
    async def save_audit_event(self, record: Mapping[str, Any]) -> None:
        await self._record("audit", record)

    async def save_audit_events(self, records: Sequence[Mapping[str, Any]]) -> None:
        await self._record("audit", records)

    async def save_market_snapshot(self, record: Mapping[str, Any]) -> None:
        await self._record("market", record)

    async def save_market_snapshots(self, records: Sequence[Mapping[str, Any]]) -> None:
        await self._record("market", records)

    async def save_orderbook_snapshot(self, record: Mapping[str, Any]) -> None:
        await self._record("orderbook", record)

    async def save_orderbook_snapshots(self, records: Sequence[Mapping[str, Any]]) -> None:
        await self._record("orderbook", records)

    async def save_order(self, record: Mapping[str, Any]) -> None:
        await self._record("order", record)

    async def save_orders(self, records: Sequence[Mapping[str, Any]]) -> None:
        await self._record("order", records)

    async def save_fill(self, record: Mapping[str, Any]) -> None:
        await self._record("fill", record)

    async def save_fills(self, records: Sequence[Mapping[str, Any]]) -> None:
        await self._record("fill", records)

    async def save_position(self, record: Mapping[str, Any]) -> None:
        await self._record("position", record)

    async def save_positions(self, records: Sequence[Mapping[str, Any]]) -> None:
        await self._record("position", records)

    async def save_allocation(self, record: Mapping[str, Any]) -> None:
        await self._record("allocation", record)

    async def save_allocations(self, records: Sequence[Mapping[str, Any]]) -> None:
        await self._record("allocation", records)

    async def save_decision_record(self, record: Mapping[str, Any]) -> None:
        await self._record("decision", record)

    async def save_decision_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        await self._record("decision", records)

    async def save_outbox_event(self, record: Mapping[str, Any]) -> None:
        await self._record("outbox", record)

    async def save_outbox_events(self, records: Sequence[Mapping[str, Any]]) -> None:
        await self._record("outbox", records)


def _make_audit_event(
    *,
    event_type: str = DomainEventType.MARKET_FILTERED_OUT.value,
    trace_id: str = "trace-1",
    condition_id: str | None = "cond-1",
    priority: int = 3,
    retry_count: int = 0,
    event_id: str = "",
) -> OutboxEvent:
    return OutboxEvent(
        trace_id=trace_id,
        event_type=event_type,
        idempotency_key=f"idem-{trace_id}-{event_type}",
        event_id=event_id or f"evt-{trace_id}-{event_type}",
        condition_id=condition_id,
        priority=priority,
        retry_count=retry_count,
        payload={"reason": "skipped_low_liquidity"},
    )


def _make_worker(
    *,
    outbox: _StubOutbox,
    repository: _StubRepository,
    max_retry_count: int = 3,
    batch_size: int = 64,
) -> PersistenceWorker:
    return PersistenceWorker(
        strategy_id="sports_tail",
        outbox=outbox,
        repository=repository,
        batch_size=batch_size,
        poll_timeout_s=0.05,
        drain_timeout_s=0.01,
        low_priority_merge_window_s=0.01,
        max_retry_count=max_retry_count,
    )


# ============================================================
# run_once：基本路径
# ============================================================

def test_run_once_returns_none_when_outbox_empty() -> None:
    outbox = _StubOutbox()
    repo = _StubRepository()
    worker = _make_worker(outbox=outbox, repository=repo)

    result = asyncio.run(worker.run_once())
    assert result is None
    assert repo.calls == []
    assert outbox.acked == []


def test_run_once_persists_audit_event_and_acks() -> None:
    """非 order/fill 事件路由：audit + outbox 两条 record。成功 → ack。"""
    event = _make_audit_event()
    outbox = _StubOutbox([event])
    repo = _StubRepository()
    worker = _make_worker(outbox=outbox, repository=repo)

    result = asyncio.run(worker.run_once())
    assert result is not None
    assert result.batch_size == 1
    assert result.failed_events == 0
    # repository 至少写了 audit 和 outbox 两 kind
    written_kinds = {kind for kind, _payload in repo.calls}
    assert "audit" in written_kinds
    assert "outbox" in written_kinds
    # 成功 → outbox.ack 被调用
    assert len(outbox.acked) == 1
    assert outbox.acked[0].event_id == event.event_id
    assert outbox.retried == [] and outbox.dead_lettered == []


def test_order_event_routes_to_order_and_audit_kinds() -> None:
    """ORDER_STATE_UPDATED 既要 save_order 也要 save_audit_event + save_outbox。"""
    event = OutboxEvent(
        trace_id="trace-order",
        event_type=DomainEventType.ORDER_STATE_UPDATED.value,
        idempotency_key="idem-order-1",
        event_id="evt-order-1",
        condition_id="cond-1",
        token_id="tok-1",
        priority=1,  # critical
        payload={
            "order": {
                "order_id": "exchange-order-id-1",
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "side": "BUY",
                "order_type": "FAK",
                "price": "0.5",
                "status": "live",
                "trace_id": "trace-order",
            },
        },
    )
    outbox = _StubOutbox([event])
    repo = _StubRepository()
    worker = _make_worker(outbox=outbox, repository=repo)

    result = asyncio.run(worker.run_once())
    assert result is not None
    written_kinds = {kind for kind, _payload in repo.calls}
    # ORDER 事件应该至少写 audit + order + outbox 三 kind
    assert {"audit", "order", "outbox"}.issubset(written_kinds)


# ============================================================
# 失败处理：retry / dead_letter
# ============================================================

def test_repository_failure_retries_when_under_max() -> None:
    """retryable failure + retry_count < max_retry → outbox.retry，不 ack 也不 dead_letter。"""
    event = _make_audit_event(retry_count=1)
    outbox = _StubOutbox([event])
    # audit kind 直接失败；让 worker 进 retry 分支
    repo = _StubRepository(
        fail_on_kinds={"audit"},
        fail_exc=ConnectionError("transient_pg_error"),  # ConnectionError 应被视为 retryable
    )
    worker = _make_worker(outbox=outbox, repository=repo, max_retry_count=3)

    result = asyncio.run(worker.run_once())
    assert result is not None
    assert result.failed_events == 1
    # retry 被触发，dead_letter 没有
    assert len(outbox.retried) == 1
    assert outbox.dead_lettered == []
    assert outbox.acked == []


def test_repository_failure_dead_letters_after_max_retries() -> None:
    """retry_count >= max_retry → 即使是 retryable error 也走 dead_letter。"""
    event = _make_audit_event(retry_count=5)  # 远超 max_retry=3
    outbox = _StubOutbox([event])
    repo = _StubRepository(
        fail_on_kinds={"audit"},
        fail_exc=ConnectionError("transient_pg_error"),
    )
    worker = _make_worker(outbox=outbox, repository=repo, max_retry_count=3)

    result = asyncio.run(worker.run_once())
    assert result is not None
    assert result.failed_events == 1
    assert outbox.retried == []
    assert len(outbox.dead_lettered) == 1
    assert outbox.acked == []


# ============================================================
# 合并 / coalesce
# ============================================================

def test_low_priority_events_with_same_route_key_coalesce() -> None:
    """同 (event_type, market_slug, condition_id, token_id) 低优先级事件 → 仅保留最后一条。
    CLAUDE.md §7 提到"低优先级快照可降采样"。"""
    # 同一 condition_id 下连续 3 条 MARKET_UPDATED（priority=3 低）
    e1 = _make_audit_event(
        event_type=DomainEventType.MARKET_UPDATED.value, event_id="evt-m-1"
    )
    e2 = _make_audit_event(
        event_type=DomainEventType.MARKET_UPDATED.value, event_id="evt-m-2"
    )
    e3 = _make_audit_event(
        event_type=DomainEventType.MARKET_UPDATED.value, event_id="evt-m-3"
    )
    outbox = _StubOutbox([e1, e2, e3])
    repo = _StubRepository()
    # batch_size 大，drain_timeout 足够让 3 条都进同一批
    worker = PersistenceWorker(
        strategy_id="sports_tail",
        outbox=outbox,
        repository=repo,
        batch_size=64,
        poll_timeout_s=0.05,
        drain_timeout_s=0.05,
        low_priority_merge_window_s=0.05,
        max_retry_count=3,
    )

    result = asyncio.run(worker.run_once())
    assert result is not None
    assert result.merged_events == 2, "3 个同 route_key 低优先事件应合并到 1 → merged_events=2"
    # 三条都被处理（ack 或合并消化）；最终 acked 至少包含一条
    assert len(outbox.acked) >= 1


def test_critical_events_do_not_coalesce() -> None:
    """priority<=1 关键事件即使 route_key 一样也必须各自保留。"""
    e1 = _make_audit_event(
        event_type=DomainEventType.ORDER_STATE_UPDATED.value,
        event_id="evt-c-1",
        priority=0,  # critical
    )
    e2 = _make_audit_event(
        event_type=DomainEventType.ORDER_STATE_UPDATED.value,
        event_id="evt-c-2",
        priority=0,
    )
    outbox = _StubOutbox([e1, e2])
    repo = _StubRepository()
    worker = PersistenceWorker(
        strategy_id="sports_tail",
        outbox=outbox,
        repository=repo,
        batch_size=64,
        poll_timeout_s=0.05,
        drain_timeout_s=0.05,
        low_priority_merge_window_s=0.05,
        max_retry_count=3,
    )

    result = asyncio.run(worker.run_once())
    assert result is not None
    assert result.merged_events == 0, "关键事件不应被合并"


# ============================================================
# snapshot 反映统计
# ============================================================

def test_snapshot_reflects_cumulative_stats() -> None:
    """处理一批事件后 snapshot 反映 processed/persisted/路由计数；无失败时 last_error 是 None。"""
    # 不同 route_key（不同 event_type）避免被合并，保证 batch 包 2 条
    event_a = _make_audit_event(
        event_type=DomainEventType.MARKET_FILTERED_IN.value, event_id="evt-snap-1"
    )
    event_b = _make_audit_event(
        event_type=DomainEventType.MARKET_FILTERED_OUT.value, event_id="evt-snap-2"
    )
    outbox = _StubOutbox([event_a, event_b])
    repo = _StubRepository()
    # drain_timeout 设宽松一点让第二个事件被同批吃掉
    worker = PersistenceWorker(
        strategy_id="sports_tail",
        outbox=outbox,
        repository=repo,
        batch_size=64,
        poll_timeout_s=0.05,
        drain_timeout_s=0.05,
        low_priority_merge_window_s=0.05,
        max_retry_count=3,
    )

    result = asyncio.run(worker.run_once())
    assert result is not None
    assert result.batch_size == 2

    snap = worker.snapshot()
    assert snap.processed_events == 2
    assert snap.persisted_events == 2
    assert snap.last_persisted_at is not None
    assert snap.last_error is None
    # 路由计数包含 audit 与 outbox
    kinds = dict(snap.route_write_counts)
    assert kinds.get("audit", 0) >= 2
    assert kinds.get("outbox", 0) >= 2


# ============================================================
# 配置保护
# ============================================================

def test_worker_requires_strategy_id() -> None:
    """空 strategy_id 必须 raise——避免无归属审计记录。"""
    with pytest.raises(ValueError):
        PersistenceWorker(
            strategy_id="",
            outbox=_StubOutbox(),
            repository=_StubRepository(),
        )
