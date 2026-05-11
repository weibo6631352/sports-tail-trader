from __future__ import annotations

from typing import Any, AsyncIterator, Iterable, Sequence

from sqlalchemy import select

from polymarket_trader.domain.order import Order, OrderResult
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import (
    OrderModel,
    PositionModel,
)
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
