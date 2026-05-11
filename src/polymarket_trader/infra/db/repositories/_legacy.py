from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, AsyncIterator, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.order import Order, OrderResult
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import (
    AccountSnapshotModel,
    FillModel,
    OrderModel,
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
