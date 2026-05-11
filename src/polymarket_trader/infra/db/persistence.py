from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.infra.db.record_mappers import (
    _text,
    account_snapshot_from_record,
    allocation_from_record,
    audit_event_from_record,
    decision_record_from_record,
    fill_from_record,
    market_from_record,
    order_from_record,
    orderbook_from_record,
    outbox_event_from_record,
    position_from_record,
)
from polymarket_trader.infra.db.repositories import (
    AccountSnapshotRepository,
    AllocationRepository,
    AuditEventRepository,
    DecisionRecordRepository,
    FillRepository,
    MarketRepository,
    OrderRepository,
    OrderbookSnapshotRepository,
    OutboxEventRepository,
    PositionRepository,
)


@dataclass(frozen=True, slots=True)
class _RepositoryGroup:
    account: AccountSnapshotRepository
    audit: AuditEventRepository
    market: MarketRepository
    orderbook: OrderbookSnapshotRepository
    allocation: AllocationRepository
    order: OrderRepository
    fill: FillRepository
    position: PositionRepository
    decision: DecisionRecordRepository
    outbox: OutboxEventRepository


class DatabasePersistenceRepository:
    """把 P3 持久化记录桥接到 typed repository。

    `PersistenceWorker` 消费的是结构化字典记录，而 DB 仓储要求 domain DTO。
    这里集中做一次字段收敛和 session/commit 管理，避免把转换逻辑塞回交易热路径。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_audit_event(self, record: Mapping[str, Any]) -> int:
        return await self.save_audit_events([record])

    async def save_audit_events(self, records: Sequence[Mapping[str, Any]]) -> int:
        audit_events: list[AuditEvent] = []
        raw_payloads: list[dict[str, Any]] = []
        for record in records:
            audit_event = audit_event_from_record(record)
            if audit_event is None:
                continue
            audit_events.append(audit_event)
            raw_payloads.append(dict(record))
        if not audit_events:
            return 0
        return await self._with_repositories(
            lambda repos: repos.audit.save_audit_events(audit_events, raw_payloads=raw_payloads)
        )

    async def save_market_snapshot(self, record: Mapping[str, Any]) -> int:
        return await self.save_market_snapshots([record])

    async def save_account_snapshot(self, record: Mapping[str, Any]) -> int:
        return await self.save_account_snapshots([record])

    async def save_account_snapshots(self, records: Sequence[Mapping[str, Any]]) -> int:
        grouped: dict[tuple[str | None], list[tuple[AccountSnapshot, dict[str, Any]]]] = {}
        for record in records:
            snapshot = account_snapshot_from_record(record)
            if snapshot is None:
                continue
            key = (_text(record.get("trace_id")),)
            grouped.setdefault(key, []).append((snapshot, dict(record)))
        if not grouped:
            return 0

        async def write(repos: _RepositoryGroup) -> int:
            total = 0
            for (trace_id,), items in grouped.items():
                total += await repos.account.save_snapshots(
                    [item[0] for item in items],
                    trace_id=trace_id,
                    raw_payloads=[item[1] for item in items],
                )
            return total

        return await self._with_repositories(write)

    async def save_market_snapshots(self, records: Sequence[Mapping[str, Any]]) -> int:
        grouped: dict[tuple[str | None, str | None], list[tuple[Market, dict[str, Any]]]] = {}
        for record in records:
            market = market_from_record(record)
            if market is None:
                continue
            key = (_text(record.get("trace_id")), _text(record.get("source")))
            grouped.setdefault(key, []).append((market, dict(record)))
        if not grouped:
            return 0

        async def write(repos: _RepositoryGroup) -> int:
            total = 0
            for (trace_id, source), items in grouped.items():
                total += await repos.market.save_markets(
                    [item[0] for item in items],
                    trace_id=trace_id,
                    source=source,
                    raw_payloads=[item[1] for item in items],
                )
            return total

        return await self._with_repositories(write)

    async def save_orderbook_snapshot(self, record: Mapping[str, Any]) -> int:
        return await self.save_orderbook_snapshots([record])

    async def save_orderbook_snapshots(self, records: Sequence[Mapping[str, Any]]) -> int:
        grouped: dict[
            tuple[str | None, str | None],
            list[tuple[OrderbookSnapshot, dict[str, Any]]],
        ] = {}
        for record in records:
            snapshot = orderbook_from_record(record)
            if snapshot is None:
                continue
            key = (_text(record.get("trace_id")), _text(record.get("source")))
            grouped.setdefault(key, []).append((snapshot, dict(record)))
        if not grouped:
            return 0

        async def write(repos: _RepositoryGroup) -> int:
            total = 0
            for (trace_id, source), items in grouped.items():
                total += await repos.orderbook.save_snapshots(
                    [item[0] for item in items],
                    trace_id=trace_id,
                    source=source,
                    raw_payloads=[item[1] for item in items],
                )
            return total

        return await self._with_repositories(write)

    async def save_order(self, record: Mapping[str, Any]) -> int:
        return await self.save_orders([record])

    async def save_orders(self, records: Sequence[Mapping[str, Any]]) -> int:
        orders: list[Order] = []
        raw_payloads: list[dict[str, Any]] = []
        for record in records:
            order = order_from_record(record)
            if order is None:
                continue
            orders.append(order)
            raw_payloads.append(dict(record))
        if not orders:
            return 0
        return await self._with_repositories(
            lambda repos: repos.order.save_orders(orders, raw_payloads=raw_payloads)
        )

    async def save_fill(self, record: Mapping[str, Any]) -> int:
        return await self.save_fills([record])

    async def save_fills(self, records: Sequence[Mapping[str, Any]]) -> int:
        fills: list[Fill] = []
        raw_payloads: list[dict[str, Any]] = []
        for record in records:
            fill = fill_from_record(record)
            if fill is None:
                continue
            fills.append(fill)
            raw_payloads.append(dict(record))
        if not fills:
            return 0
        return await self._with_repositories(
            lambda repos: repos.fill.save_fills(fills, raw_payloads=raw_payloads)
        )

    async def save_position(self, record: Mapping[str, Any]) -> int:
        return await self.save_positions([record])

    async def save_positions(self, records: Sequence[Mapping[str, Any]]) -> int:
        grouped: dict[tuple[str | None], list[Position]] = {}
        raw_payloads: dict[tuple[str | None], list[dict[str, Any]]] = {}
        for record in records:
            position = position_from_record(record)
            if position is None:
                continue
            key = (_text(record.get("trace_id")),)
            grouped.setdefault(key, []).append(position)
            raw_payloads.setdefault(key, []).append(dict(record))
        if not grouped:
            return 0

        async def write(repos: _RepositoryGroup) -> int:
            total = 0
            for (trace_id,), positions in grouped.items():
                total += await repos.position.save_positions(
                    positions,
                    trace_id=trace_id,
                    raw_payloads=raw_payloads[(trace_id,)],
                )
            return total

        return await self._with_repositories(write)

    async def save_allocation(self, record: Mapping[str, Any]) -> int:
        return await self.save_allocations([record])

    async def save_allocations(self, records: Sequence[Mapping[str, Any]]) -> int:
        grouped: dict[tuple[str | None], list[Allocation]] = {}
        raw_payloads: dict[tuple[str | None], list[dict[str, Any]]] = {}
        for record in records:
            allocation = allocation_from_record(record)
            if allocation is None:
                continue
            key = (_text(record.get("trace_id")),)
            grouped.setdefault(key, []).append(allocation)
            raw_payloads.setdefault(key, []).append(dict(record))
        if not grouped:
            return 0

        async def write(repos: _RepositoryGroup) -> int:
            total = 0
            for (trace_id,), allocations in grouped.items():
                if trace_id is None:
                    continue
                total += await repos.allocation.save_allocations(
                    allocations,
                    trace_id=trace_id,
                    raw_payloads=raw_payloads[(trace_id,)],
                )
            return total

        return await self._with_repositories(write)

    async def save_decision_record(self, record: Mapping[str, Any]) -> int:
        return await self.save_decision_records([record])

    async def save_decision_records(self, records: Sequence[Mapping[str, Any]]) -> int:
        decisions: list[DecisionRecord] = []
        for record in records:
            decision = decision_record_from_record(record)
            if decision is None:
                continue
            decisions.append(decision)
        if not decisions:
            return 0
        return await self._with_repositories(
            lambda repos: repos.decision.save_decision_records(decisions)
        )

    async def save_outbox_event(self, record: Mapping[str, Any]) -> int:
        return await self.save_outbox_events([record])

    async def save_outbox_events(self, records: Sequence[Mapping[str, Any]]) -> int:
        events: list[OutboxEvent] = []
        raw_payloads: list[dict[str, Any]] = []
        for record in records:
            event = outbox_event_from_record(record)
            if event is None:
                continue
            events.append(event)
            raw_payloads.append(dict(record))
        if not events:
            return 0
        return await self._with_repositories(
            lambda repos: repos.outbox.save_events(events, raw_payloads=raw_payloads)
        )

    async def _with_repositories(self, callback: Any) -> int:
        async with self._session_factory() as session:
            repositories = _RepositoryGroup(
                account=AccountSnapshotRepository(session),
                audit=AuditEventRepository(session),
                market=MarketRepository(session),
                orderbook=OrderbookSnapshotRepository(session),
                allocation=AllocationRepository(session),
                order=OrderRepository(session),
                fill=FillRepository(session),
                position=PositionRepository(session),
                decision=DecisionRecordRepository(session),
                outbox=OutboxEventRepository(session),
            )
            result = await callback(repositories)
            await session.commit()
            return int(result or 0)


__all__ = ["DatabasePersistenceRepository"]
