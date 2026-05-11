from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, AsyncIterator, Iterable, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.order import Order, OrderResult
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import (
    AccountSnapshotModel,
    AllocationModel,
    AuditEventModel,
    DecisionRecordModel,
    FillModel,
    OrderModel,
    OutboxEventModel,
    PositionModel,
    _decimal,
    _ensure_aware,
)
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
    _limit_offset,
    _row_dict,
)


class AllocationRepository(BaseRepository):
    """分配计划仓储。"""

    async def save_allocation(
        self,
        allocation: Allocation,
        *,
        trace_id: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> Allocation:
        await self.save_allocations([allocation], trace_id=trace_id, raw_payloads=[raw_payload])
        return allocation

    async def save_allocations(
        self,
        allocations: Iterable[Allocation],
        *,
        trace_id: str,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        allocations = tuple(allocations)
        payloads = raw_payloads or (None,) * len(allocations)
        rows = [
            _row_dict(AllocationModel.from_domain(allocation, trace_id=trace_id, raw_payload=payload))
            for allocation, payload in zip(allocations, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            AllocationModel,
            rows,
            conflict_columns=("allocation_key",),
            update_columns=(
                "strategy_id",
                "trace_id",
                "condition_id",
                "market_slug",
                "token_id",
                "target_budget_usdc",
                "buy_budget_usdc",
                "current_exposure_usdc",
                "released_budget_usdc",
                "reason",
                "release_reason",
                "idempotency_key",
                "raw_payload",
                "updated_at",
            ),
        )

    async def list_allocations_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
        strategy_id: str | None = None,
    ) -> RepositoryPage[Allocation]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(AllocationModel).order_by(AllocationModel.updated_at.desc(), AllocationModel.id.desc())
        if trace_id is not None:
            stmt = stmt.where(AllocationModel.trace_id == trace_id)
        if condition_id is not None:
            stmt = stmt.where(AllocationModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(AllocationModel.token_id == token_id)
        if market_slug is not None:
            stmt = stmt.where(AllocationModel.market_slug == market_slug)
        if strategy_id is not None:
            stmt = stmt.where(AllocationModel.strategy_id == strategy_id)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)


class OrderRepository(BaseRepository):
    """订单状态仓储。"""

    async def save_order(self, order: Order | OrderResult, *, raw_payload: dict[str, Any] | None = None) -> Order | OrderResult:
        await self.save_orders([order], raw_payloads=[raw_payload])
        return order

    async def save_orders(
        self,
        orders: Iterable[Order | OrderResult],
        *,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        orders = tuple(orders)
        payloads = raw_payloads or (None,) * len(orders)
        rows = [
            _row_dict(OrderModel.from_domain(order, raw_payload=payload))
            for order, payload in zip(orders, payloads, strict=False)
        ]
        # 交易所订单号出现后，它就是订单快照的权威身份；本地 idempotency/trace 只用于
        # 订单未提交前的暂态记录，避免同一 exchange order 在重放时撞上 order_id 唯一约束。
        exchange_rows = tuple(row for row in rows if row.get("order_id"))
        local_rows = tuple(row for row in rows if not row.get("order_id"))
        update_columns = (
            "order_key",
            "strategy_id",
            "trace_id",
            "condition_id",
            "token_id",
            "market_slug",
            "side",
            "order_type",
            "price",
            "amount_usdc",
            "size_shares",
            "filled_shares",
            "remaining_shares",
            "notional_usdc",
            "order_id",
            "trade_id",
            "status",
            "idempotency_key",
            "reason",
            "post_only",
            "raw_payload",
            "updated_at",
        )
        total = 0
        total += await self._bulk_upsert(
            OrderModel,
            exchange_rows,
            conflict_columns=("order_id",),
            update_columns=update_columns,
        )
        total += await self._bulk_upsert(
            OrderModel,
            local_rows,
            conflict_columns=("order_key",),
            update_columns=update_columns,
        )
        return total

    async def get_order(self, order_id: str) -> Order | None:
        row = await self._session.scalar(select(OrderModel).where(OrderModel.order_id == order_id))
        return None if row is None else row.to_domain()

    async def list_open_orders_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        condition_id: str | None = None,
        token_id: str | None = None,
        strategy_id: str | None = None,
    ) -> RepositoryPage[Order]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(OrderModel).where(
            OrderModel.status.in_(
                [
                    "created",
                    "signed",
                    "submitted",
                    "cancel_requested",
                    "live",
                    "matched",
                    "partially_filled",
                ]
            )
        )
        if condition_id is not None:
            stmt = stmt.where(OrderModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(OrderModel.token_id == token_id)
        if strategy_id is not None:
            stmt = stmt.where(OrderModel.strategy_id == strategy_id)
        stmt = stmt.order_by(OrderModel.updated_at.desc(), OrderModel.id.desc())
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)

    async def list_orders_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        order_id: str | None = None,
        trade_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        time_range: TimeRange | None = None,
        strategy_id: str | None = None,
    ) -> RepositoryPage[Order]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(OrderModel).order_by(OrderModel.updated_at.desc(), OrderModel.id.desc())
        if trace_id is not None:
            stmt = stmt.where(OrderModel.trace_id == trace_id)
        if order_id is not None:
            stmt = stmt.where(OrderModel.order_id == order_id)
        if trade_id is not None:
            stmt = stmt.where(OrderModel.trade_id == trade_id)
        if condition_id is not None:
            stmt = stmt.where(OrderModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(OrderModel.token_id == token_id)
        if strategy_id is not None:
            stmt = stmt.where(OrderModel.strategy_id == strategy_id)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(OrderModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(OrderModel.created_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)

    async def stream_orders_in_range(
        self,
        *,
        time_range: TimeRange | None,
        limit: int,
    ) -> AsyncIterator[OrderModel]:
        """按 updated_at 时间窗流式拉取订单 ORM 行，供 export 端点消费。

        使用 stream_scalars 走 server-side cursor，避免一次性把全表读进内存。
        """

        stmt = select(OrderModel).order_by(OrderModel.updated_at.asc(), OrderModel.id.asc())
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(OrderModel.updated_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(OrderModel.updated_at <= until_dt)
        stmt = stmt.limit(limit)
        result = await self._session.stream_scalars(stmt)
        async for row in result:
            yield row


class FillRepository(BaseRepository):
    """成交记录仓储。"""

    async def save_fill(self, fill: Fill, *, raw_payload: dict[str, Any] | None = None) -> Fill:
        await self.save_fills([fill], raw_payloads=[raw_payload])
        return fill

    async def save_fills(
        self,
        fills: Iterable[Fill],
        *,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        fills = tuple(fills)
        payloads = raw_payloads or (None,) * len(fills)
        rows = [
            _row_dict(FillModel.from_domain(fill, raw_payload=payload))
            for fill, payload in zip(fills, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            FillModel,
            rows,
            conflict_columns=("event_id",),
            update_columns=(
                "strategy_id",
                "trace_id",
                "event_type",
                "condition_id",
                "token_id",
                "market_slug",
                "order_id",
                "trade_id",
                "side",
                "price",
                "size",
                "notional_usdc",
                "status",
                "confirmed_at",
                "raw_payload",
                "updated_at",
            ),
        )

    async def list_fills_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        order_id: str | None = None,
        trade_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        time_range: TimeRange | None = None,
        strategy_id: str | None = None,
    ) -> RepositoryPage[Fill]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(FillModel).order_by(FillModel.confirmed_at.desc(), FillModel.id.desc())
        if trace_id is not None:
            stmt = stmt.where(FillModel.trace_id == trace_id)
        if order_id is not None:
            stmt = stmt.where(FillModel.order_id == order_id)
        if trade_id is not None:
            stmt = stmt.where(FillModel.trade_id == trade_id)
        if condition_id is not None:
            stmt = stmt.where(FillModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(FillModel.token_id == token_id)
        if strategy_id is not None:
            stmt = stmt.where(FillModel.strategy_id == strategy_id)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(FillModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(FillModel.created_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)

    async def stream_fills_in_range(
        self,
        *,
        time_range: TimeRange | None,
        limit: int,
    ) -> AsyncIterator[FillModel]:
        """按 confirmed_at 时间窗流式拉取成交 ORM 行。

        confirmed_at 可空：对 NULL 行用 created_at 兜底，确保未确认 fill 也能被
        导出窗口看到，不至于在审计时被静默丢掉。
        """

        time_column = func.coalesce(FillModel.confirmed_at, FillModel.created_at)
        stmt = select(FillModel).order_by(time_column.asc(), FillModel.id.asc())
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(time_column >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(time_column <= until_dt)
        stmt = stmt.limit(limit)
        result = await self._session.stream_scalars(stmt)
        async for row in result:
            yield row


class PositionRepository(BaseRepository):
    """持仓仓储。"""

    async def save_position(
        self,
        position: Position,
        *,
        trace_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> Position:
        await self.save_positions([position], trace_id=trace_id, raw_payloads=[raw_payload])
        return position

    async def save_positions(
        self,
        positions: Iterable[Position],
        *,
        trace_id: str | None = None,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        positions = tuple(positions)
        payloads = raw_payloads or (None,) * len(positions)
        rows = [
            _row_dict(PositionModel.from_domain(position, trace_id=trace_id, raw_payload=payload))
            for position, payload in zip(positions, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            PositionModel,
            rows,
            conflict_columns=("position_key",),
            update_columns=(
                "strategy_id",
                "trace_id",
                "condition_id",
                "token_id",
                "market_slug",
                "shares",
                "cost_usdc",
                "open_buy_shares",
                "open_sell_shares",
                "pending_buy_shares",
                "confirmed_shares",
                "last_order_id",
                "last_trade_id",
                "confirmation_status",
                "avg_price",
                "initial_value",
                "current_value",
                "cash_pnl",
                "percent_pnl",
                "realized_pnl",
                "percent_realized_pnl",
                "cur_price",
                "redeemable",
                "raw_payload",
                "updated_at",
            ),
        )

    async def list_positions_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        condition_id: str | None = None,
        token_id: str | None = None,
        strategy_id: str | None = None,
    ) -> RepositoryPage[Position]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(PositionModel).order_by(PositionModel.updated_at.desc(), PositionModel.id.desc())
        if condition_id is not None:
            stmt = stmt.where(PositionModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(PositionModel.token_id == token_id)
        if strategy_id is not None:
            stmt = stmt.where(PositionModel.strategy_id == strategy_id)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)

    async def list_by_condition_ids(
        self,
        condition_ids: Sequence[str],
        *,
        strategy_id: str | None = None,
    ) -> tuple[Position, ...]:
        """按 ``condition_id`` 集合批量取仓位——给跨表聚合（edge-realization 等）用，
        避免 N+1。无 condition_ids 时返回空 tuple。"""

        ids = tuple({cid for cid in condition_ids if cid})
        if not ids:
            return ()
        stmt = select(PositionModel).where(PositionModel.condition_id.in_(ids))
        if strategy_id is not None:
            stmt = stmt.where(PositionModel.strategy_id == strategy_id)
        result = await self._session.scalars(stmt)
        return tuple(row.to_domain() for row in result.all())


@dataclass(frozen=True, slots=True)
class AccountHistoryPoint:
    """聚合后的账户净值时间序列点。

    ``recorded_at`` 是当前 bucket 内最新一条 snapshot 的写入时间，便于
    drawdown 计算回到原始时间轴上。
    """

    recorded_at: datetime
    net_value_usdc: Decimal


class AccountSnapshotRepository(BaseRepository):
    """账户余额快照仓储——append-only 时间序列。

    每次 ``save_snapshot`` 都插入一行新记录。读取"当前账户状态"必须按
    ``recorded_at DESC LIMIT 1`` 查最新一行；``query_history_bucketed`` 用
    ``date_trunc`` 做服务端 downsampling，避免把原始点全部拉到 Python。
    """

    async def save_snapshot(
        self,
        snapshot: AccountSnapshot,
        *,
        trace_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
        account_key: str = "primary",
        recorded_at: datetime | None = None,
        net_value_usdc: Decimal | None = None,
    ) -> AccountSnapshot:
        await self.save_snapshots(
            [snapshot],
            trace_id=trace_id,
            raw_payloads=[raw_payload],
            account_key=account_key,
            recorded_at=recorded_at,
            net_values_usdc=None if net_value_usdc is None else [net_value_usdc],
        )
        return snapshot

    async def save_snapshots(
        self,
        snapshots: Iterable[AccountSnapshot],
        *,
        trace_id: str | None = None,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
        account_key: str = "primary",
        recorded_at: datetime | None = None,
        net_values_usdc: Sequence[Decimal | None] | None = None,
    ) -> int:
        snapshots = tuple(snapshots)
        if not snapshots:
            return 0
        payloads = raw_payloads or (None,) * len(snapshots)
        net_values = net_values_usdc or (None,) * len(snapshots)
        rows = [
            _row_dict(
                AccountSnapshotModel.from_domain(
                    snapshot,
                    trace_id=trace_id,
                    raw_payload=payload,
                    account_key=account_key,
                    recorded_at=recorded_at,
                    net_value_usdc=net_value,
                )
            )
            for snapshot, payload, net_value in zip(
                snapshots, payloads, net_values, strict=False
            )
        ]
        # append-only：直接 INSERT，没有 ON CONFLICT 收敛。
        stmt = pg_insert(AccountSnapshotModel).values(list(rows))
        await self._session.execute(stmt)
        return len(rows)

    async def get_current_snapshot(self, *, account_key: str = "primary") -> AccountSnapshot | None:
        stmt = (
            select(AccountSnapshotModel)
            .where(AccountSnapshotModel.account_key == account_key)
            .order_by(AccountSnapshotModel.recorded_at.desc())
            .limit(1)
        )
        row = await self._session.scalar(stmt)
        return None if row is None else row.to_domain()

    async def query_history_bucketed(
        self,
        *,
        since: datetime,
        until: datetime,
        interval_ms: int,
        account_key: str = "primary",
    ) -> tuple[AccountHistoryPoint, ...]:
        """按 ``interval_ms`` 服务器端 downsampling 净值时间序列。

        实现：用 ``to_timestamp(floor(epoch / interval_s) * interval_s)`` 计算
        bucket，每个 bucket 内取最新一行。所有聚合在 PG 内完成，Python 端只拿
        ``≈ window/interval`` 条结果。
        """

        if interval_ms <= 0:
            raise ValueError("interval_ms must be > 0")
        since_aware = _ensure_aware(since)
        until_aware = _ensure_aware(until)
        interval_s = interval_ms / 1000.0
        bucket_expr = func.to_timestamp(
            func.floor(func.extract("epoch", AccountSnapshotModel.recorded_at) / interval_s)
            * interval_s
        ).label("bucket_start")
        # DISTINCT ON bucket：每个 bucket 取 recorded_at 最大那一行。
        stmt = (
            select(
                bucket_expr,
                AccountSnapshotModel.recorded_at,
                AccountSnapshotModel.net_value_usdc,
            )
            .where(AccountSnapshotModel.account_key == account_key)
            .where(AccountSnapshotModel.recorded_at >= since_aware)
            .where(AccountSnapshotModel.recorded_at <= until_aware)
            .distinct(bucket_expr)
            .order_by(bucket_expr, AccountSnapshotModel.recorded_at.desc())
        )
        result = await self._session.execute(stmt)
        points: list[AccountHistoryPoint] = []
        for _bucket_start, recorded_at, net_value in result.all():
            points.append(
                AccountHistoryPoint(
                    recorded_at=_ensure_aware(recorded_at),
                    net_value_usdc=_decimal(net_value) or Decimal("0"),
                )
            )
        points.sort(key=lambda point: point.recorded_at)
        return tuple(points)


class AuditEventRepository(BaseRepository):
    """审计事件仓储。"""

    async def save_audit_event(self, event: AuditEvent, *, raw_payload: dict[str, Any] | None = None) -> AuditEvent:
        await self.save_audit_events([event], raw_payloads=[raw_payload])
        return event

    async def save_audit_events(
        self,
        events: Iterable[AuditEvent],
        *,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        events = tuple(events)
        payloads = raw_payloads or (None,) * len(events)
        rows = [
            _row_dict(AuditEventModel.from_domain(event, raw_payload=payload))
            for event, payload in zip(events, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            AuditEventModel,
            rows,
            conflict_columns=("event_id",),
            update_columns=(
                "strategy_id",
                "trace_id",
                "event_title",
                "market_slug",
                "event_slug",
                "condition_id",
                "token_id",
                "outcome",
                "side",
                "order_type",
                "price",
                "size",
                "notional_usdc",
                "order_id",
                "trade_id",
                "tx_hash",
                "status",
                "reason",
                "raw_response",
                "payload",
                "updated_at",
            ),
        )

    async def list_audit_events_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        event_title: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        time_range: TimeRange | None = None,
        strategy_id: str | None = None,
    ) -> RepositoryPage[AuditEvent]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(AuditEventModel).order_by(AuditEventModel.created_at.desc(), AuditEventModel.id.desc())
        if trace_id is not None:
            stmt = stmt.where(AuditEventModel.trace_id == trace_id)
        if event_title is not None:
            stmt = stmt.where(AuditEventModel.event_title == event_title)
        if condition_id is not None:
            stmt = stmt.where(AuditEventModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(AuditEventModel.token_id == token_id)
        if strategy_id is not None:
            stmt = stmt.where(AuditEventModel.strategy_id == strategy_id)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)

    async def stream_audit_events_in_range(
        self,
        *,
        time_range: TimeRange | None,
        limit: int,
    ) -> AsyncIterator[AuditEventModel]:
        """按 created_at 时间窗流式拉取审计事件 ORM 行，供 export 端点消费。"""

        stmt = select(AuditEventModel).order_by(
            AuditEventModel.created_at.asc(), AuditEventModel.id.asc()
        )
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at <= until_dt)
        stmt = stmt.limit(limit)
        result = await self._session.stream_scalars(stmt)
        async for row in result:
            yield row


class DecisionRecordRepository(BaseRepository):
    """策略决策录制仓储。

    Append-only：每条决策一行；查询走 ``created_at`` 倒序，
    `dump` 端点按 trace_id / condition_id / accepted / 时间窗过滤。
    """

    async def save_decision_record(self, record: DecisionRecord) -> int:
        return await self.save_decision_records([record])

    async def get_by_record_id(self, record_id: str) -> DecisionRecord | None:
        stmt = select(DecisionRecordModel).where(DecisionRecordModel.record_id == record_id).limit(1)
        row = await self._session.scalar(stmt)
        return None if row is None else row.to_domain()

    async def save_decision_records(self, records: Iterable[DecisionRecord]) -> int:
        records = tuple(records)
        if not records:
            return 0
        rows = [_row_dict(DecisionRecordModel.from_domain(record)) for record in records]
        # ON CONFLICT DO NOTHING 防御性兜底：record_id 唯一，正常情况下不会冲突；
        # 但 outbox retry 可能重复投递同一事件，幂等忽略保证不破坏审计行。
        return await self._bulk_upsert(
            DecisionRecordModel,
            rows,
            conflict_columns=("record_id",),
            update_columns=(),
        )

    async def list_decisions_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        accepted: bool | None = None,
        time_range: TimeRange | None = None,
        strategy_id: str | None = None,
    ) -> RepositoryPage[DecisionRecord]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(DecisionRecordModel).order_by(
            DecisionRecordModel.created_at.desc(), DecisionRecordModel.id.desc()
        )
        if trace_id is not None:
            stmt = stmt.where(DecisionRecordModel.trace_id == trace_id)
        if condition_id is not None:
            stmt = stmt.where(DecisionRecordModel.condition_id == condition_id)
        if accepted is not None:
            stmt = stmt.where(DecisionRecordModel.accepted == accepted)
        if strategy_id is not None:
            stmt = stmt.where(DecisionRecordModel.strategy_id == strategy_id)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(DecisionRecordModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(DecisionRecordModel.created_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )


class OutboxEventRepository(BaseRepository):
    """本地 outbox 仓储。"""

    async def save_event(self, event: OutboxEvent, *, raw_payload: dict[str, Any] | None = None) -> OutboxEvent:
        await self.save_events([event], raw_payloads=[raw_payload])
        return event

    async def save_events(
        self,
        events: Iterable[OutboxEvent],
        *,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        events = tuple(events)
        payloads = raw_payloads or (None,) * len(events)
        rows = [
            _row_dict(OutboxEventModel.from_domain(event, raw_payload=payload))
            for event, payload in zip(events, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            OutboxEventModel,
            rows,
            conflict_columns=("idempotency_key",),
            update_columns=(
                "event_id",
                "trace_id",
                "event_type",
                "market_slug",
                "event_slug",
                "condition_id",
                "token_id",
                "reason",
                "priority",
                "retry_count",
                "last_error",
                "raw_response_summary",
                "payload",
                "updated_at",
            ),
        )

    async def list_pending_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
    ) -> RepositoryPage[OutboxEvent]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(OutboxEventModel).order_by(
            OutboxEventModel.priority.asc(),
            OutboxEventModel.created_at.asc(),
            OutboxEventModel.id.asc(),
        )
        if trace_id is not None:
            stmt = stmt.where(OutboxEventModel.trace_id == trace_id)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    async def list_events_by_types_snapshot(
        self,
        *,
        event_types: Sequence[str],
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> RepositoryPage[OutboxEvent]:
        """按 ``event_type`` 白名单分页查询 outbox 事件。

        outbox_events 表是 append-only 审计层：lifecycle / reconcile / fill 等事件
        都会先落 outbox 再被消费，本接口给上层做"按事件类型回放"用，按
        ``created_at`` 倒序。
        """

        limit, offset = _limit_offset(limit, offset)
        types = tuple(event_types or ())
        if not types:
            return RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
        stmt = (
            select(OutboxEventModel)
            .where(OutboxEventModel.event_type.in_(types))
            .order_by(
                OutboxEventModel.created_at.desc(),
                OutboxEventModel.id.desc(),
            )
        )
        if trace_id is not None:
            stmt = stmt.where(OutboxEventModel.trace_id == trace_id)
        if condition_id is not None:
            stmt = stmt.where(OutboxEventModel.condition_id == condition_id)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(OutboxEventModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(OutboxEventModel.created_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    async def list_failures_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        event_type: str | None = None,
        time_range: TimeRange | None = None,
        min_retry_count: int = 1,
    ) -> RepositoryPage[OutboxEvent]:
        """Outbox 失败/重试事件视图。

        与 ``list_pending_snapshot`` 互补：这里只回 ``retry_count >= min_retry_count``
        或 ``last_error IS NOT NULL`` 的事件，按 ``updated_at`` 倒序，用于诊断
        持久化链路的问题。
        """

        limit, offset = _limit_offset(limit, offset)
        stmt = (
            select(OutboxEventModel)
            .where(
                or_(
                    OutboxEventModel.retry_count >= max(min_retry_count, 0),
                    OutboxEventModel.last_error.isnot(None),
                )
            )
            .order_by(
                OutboxEventModel.updated_at.desc(),
                OutboxEventModel.id.desc(),
            )
        )
        if trace_id is not None:
            stmt = stmt.where(OutboxEventModel.trace_id == trace_id)
        if event_type is not None:
            stmt = stmt.where(OutboxEventModel.event_type == event_type)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(OutboxEventModel.updated_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(OutboxEventModel.updated_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )
