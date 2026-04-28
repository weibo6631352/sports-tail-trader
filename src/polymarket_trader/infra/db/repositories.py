from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Iterable, Sequence, TypeVar, cast

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order, OrderResult
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.infra.db.models import (
    AccountSnapshotModel,
    AllocationModel,
    AuditEventModel,
    Base,
    FillModel,
    MarketModel,
    OrderModel,
    OrderbookSnapshotModel,
    OutboxEventModel,
    PositionModel,
)
from polymarket_trader.domain.account import AccountSnapshot

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RepositoryPage(Generic[T]):
    items: tuple[T, ...]
    total: int
    limit: int
    offset: int


class BaseRepository:
    """仓储基类。

    仓储只负责 PostgreSQL 写读和幂等 upsert，不承担风控、策略和交易编排。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _bulk_upsert(
        self,
        model: type[Base],
        rows: Sequence[dict[str, Any]],
        *,
        conflict_columns: Sequence[str],
        update_columns: Sequence[str],
    ) -> int:
        if not rows:
            return 0
        stmt = pg_insert(model).values(list(rows))
        if update_columns:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(conflict_columns),
                set_={column: getattr(stmt.excluded, column) for column in update_columns},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_columns))
        await self._session.execute(stmt)
        return len(rows)

    async def _paginate(
        self,
        stmt: Select[Any],
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[Any], int]:
        base_stmt = stmt.order_by(None)
        total_stmt = select(func.count()).select_from(base_stmt.subquery())
        total = int((await self._session.scalar(total_stmt)) or 0)
        result = await self._session.scalars(stmt.limit(limit).offset(offset))
        return list(result.all()), total


def _limit_offset(limit: int, offset: int) -> tuple[int, int]:
    if limit <= 0:
        limit = 100
    if offset < 0:
        offset = 0
    return limit, offset


def _row_dict(model: Any) -> dict[str, Any]:
    return {key: value for key, value in vars(model).items() if not key.startswith("_sa_")}


def _market_matches_snapshot_filters(
    market: Market,
    *,
    fee_rate_bps_min: int | None = None,
    fee_rate_bps_max: int | None = None,
    maker_base_fee_bps_min: int | None = None,
    maker_base_fee_bps_max: int | None = None,
    taker_base_fee_bps_min: int | None = None,
    taker_base_fee_bps_max: int | None = None,
) -> bool:
    if fee_rate_bps_min is not None and (market.fee_rate_bps is None or market.fee_rate_bps < fee_rate_bps_min):
        return False
    if fee_rate_bps_max is not None and (market.fee_rate_bps is None or market.fee_rate_bps > fee_rate_bps_max):
        return False
    if maker_base_fee_bps_min is not None and (
        market.maker_base_fee_bps is None or market.maker_base_fee_bps < maker_base_fee_bps_min
    ):
        return False
    if maker_base_fee_bps_max is not None and (
        market.maker_base_fee_bps is None or market.maker_base_fee_bps > maker_base_fee_bps_max
    ):
        return False
    if taker_base_fee_bps_min is not None and (
        market.taker_base_fee_bps is None or market.taker_base_fee_bps < taker_base_fee_bps_min
    ):
        return False
    if taker_base_fee_bps_max is not None and (
        market.taker_base_fee_bps is None or market.taker_base_fee_bps > taker_base_fee_bps_max
    ):
        return False
    return True


def _market_snapshot_sort_value(market: Market, sort_by: str) -> object | None:
    return {
        "market_slug": market.market_slug,
        "fee_rate_bps": market.fee_rate_bps,
        "fee_rate_updated_at": market.fee_rate_updated_at,
        "maker_base_fee_bps": market.maker_base_fee_bps,
        "taker_base_fee_bps": market.taker_base_fee_bps,
    }[sort_by]


def _sort_market_snapshots(
    markets: Sequence[Market],
    *,
    sort_by: str | None = None,
    sort_direction: str = "desc",
) -> tuple[Market, ...]:
    if sort_by is None:
        return tuple(markets)
    supported = {
        "market_slug",
        "fee_rate_bps",
        "fee_rate_updated_at",
        "maker_base_fee_bps",
        "taker_base_fee_bps",
    }
    if sort_by not in supported:
        return tuple(markets)
    if sort_by == "market_slug":
        return tuple(sorted(markets, key=lambda market: market.market_slug, reverse=sort_direction == "desc"))
    present = [market for market in markets if _market_snapshot_sort_value(market, sort_by) is not None]
    missing = [market for market in markets if _market_snapshot_sort_value(market, sort_by) is None]
    present.sort(key=lambda market: market.market_slug)
    present.sort(
        key=lambda market: cast(Any, _market_snapshot_sort_value(market, sort_by)),
        reverse=sort_direction == "desc",
    )
    return tuple(present + missing)


class MarketRepository(BaseRepository):
    """市场快照仓储。"""

    async def save_market(
        self,
        market: Market,
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> Market:
        await self.save_markets([market], trace_id=trace_id, source=source, raw_payloads=[raw_payload])
        return market

    async def save_markets(
        self,
        markets: Iterable[Market],
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        markets = tuple(markets)
        payloads = raw_payloads or (None,) * len(markets)
        rows = [
            _row_dict(
                MarketModel.from_domain(
                    market,
                    trace_id=trace_id,
                    source=source,
                    raw_payload=payload,
                )
            )
            for market, payload in zip(markets, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            MarketModel,
            rows,
            conflict_columns=("condition_id",),
            update_columns=(
                "trace_id",
                "source",
                "market_slug",
                "token_ids",
                "outcomes",
                "market_name",
                "market_question",
                "event_id",
                "event_title",
                "event_slug",
                "tick_size",
                "min_order_size",
                "neg_risk",
                "fees_enabled",
                "maker_base_fee_bps",
                "taker_base_fee_bps",
                "fee_rate_bps",
                "fee_rate_updated_at",
                "category",
                "tags",
                "matched_keywords",
                "trading_status",
                "reject_reason",
                "raw_payload",
                "updated_at",
            ),
        )

    async def get_by_condition_id(self, condition_id: str) -> Market | None:
        row = await self._session.scalar(select(MarketModel).where(MarketModel.condition_id == condition_id))
        return None if row is None else row.to_domain()

    async def get_by_market_slug(self, market_slug: str) -> Market | None:
        row = await self._session.scalar(select(MarketModel).where(MarketModel.market_slug == market_slug))
        return None if row is None else row.to_domain()

    async def get_by_token_id(self, token_id: str) -> Market | None:
        row = await self._session.scalar(
            select(MarketModel).where(MarketModel.token_ids.contains([token_id]))
        )
        return None if row is None else row.to_domain()

    async def list_markets_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trading_status: str | None = None,
        fees_enabled: bool | None = None,
        fee_rate_bps_min: int | None = None,
        fee_rate_bps_max: int | None = None,
        maker_base_fee_bps_min: int | None = None,
        maker_base_fee_bps_max: int | None = None,
        taker_base_fee_bps_min: int | None = None,
        taker_base_fee_bps_max: int | None = None,
        sort_by: str | None = None,
        sort_direction: str = "desc",
    ) -> RepositoryPage[Market]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(MarketModel)
        if trading_status is not None:
            stmt = stmt.where(MarketModel.trading_status == trading_status)
        if fees_enabled is not None:
            stmt = stmt.where(MarketModel.fees_enabled.is_(fees_enabled))

        needs_domain_fee_view = any(
            value is not None
            for value in (
                fee_rate_bps_min,
                fee_rate_bps_max,
                maker_base_fee_bps_min,
                maker_base_fee_bps_max,
                taker_base_fee_bps_min,
                taker_base_fee_bps_max,
            )
        ) or sort_by in {
            "fee_rate_bps",
            "fee_rate_updated_at",
            "maker_base_fee_bps",
            "taker_base_fee_bps",
        }
        if needs_domain_fee_view:
            stmt = stmt.order_by(MarketModel.updated_at.desc(), MarketModel.id.desc())
            rows = list((await self._session.scalars(stmt)).all())
            markets = tuple(row.to_domain() for row in rows)
            filtered = tuple(
                market
                for market in markets
                if _market_matches_snapshot_filters(
                    market,
                    fee_rate_bps_min=fee_rate_bps_min,
                    fee_rate_bps_max=fee_rate_bps_max,
                    maker_base_fee_bps_min=maker_base_fee_bps_min,
                    maker_base_fee_bps_max=maker_base_fee_bps_max,
                    taker_base_fee_bps_min=taker_base_fee_bps_min,
                    taker_base_fee_bps_max=taker_base_fee_bps_max,
                )
            )
            sorted_items = _sort_market_snapshots(
                filtered,
                sort_by=sort_by,
                sort_direction=sort_direction,
            )
            page_items = sorted_items[offset : offset + limit]
            return RepositoryPage(
                items=tuple(page_items),
                total=len(filtered),
                limit=limit,
                offset=offset,
            )

        if fee_rate_bps_min is not None:
            stmt = stmt.where(MarketModel.fee_rate_bps >= fee_rate_bps_min)
        if fee_rate_bps_max is not None:
            stmt = stmt.where(MarketModel.fee_rate_bps <= fee_rate_bps_max)
        if maker_base_fee_bps_min is not None:
            stmt = stmt.where(MarketModel.maker_base_fee_bps >= maker_base_fee_bps_min)
        if maker_base_fee_bps_max is not None:
            stmt = stmt.where(MarketModel.maker_base_fee_bps <= maker_base_fee_bps_max)
        if taker_base_fee_bps_min is not None:
            stmt = stmt.where(MarketModel.taker_base_fee_bps >= taker_base_fee_bps_min)
        if taker_base_fee_bps_max is not None:
            stmt = stmt.where(MarketModel.taker_base_fee_bps <= taker_base_fee_bps_max)

        sort_column = {
            "market_slug": MarketModel.market_slug,
            "fee_rate_bps": MarketModel.fee_rate_bps,
            "fee_rate_updated_at": MarketModel.fee_rate_updated_at,
            "maker_base_fee_bps": MarketModel.maker_base_fee_bps,
            "taker_base_fee_bps": MarketModel.taker_base_fee_bps,
        }.get(sort_by or "")
        if sort_column is None:
            stmt = stmt.order_by(MarketModel.updated_at.desc(), MarketModel.id.desc())
        elif sort_direction == "asc":
            stmt = stmt.order_by(sort_column.asc().nullslast(), MarketModel.market_slug.asc(), MarketModel.id.desc())
        else:
            stmt = stmt.order_by(sort_column.desc().nullslast(), MarketModel.market_slug.asc(), MarketModel.id.desc())
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)


class OrderbookSnapshotRepository(BaseRepository):
    """orderbook 快照仓储。"""

    async def save_snapshot(
        self,
        snapshot: OrderbookSnapshot,
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> OrderbookSnapshot:
        await self.save_snapshots([snapshot], trace_id=trace_id, source=source, raw_payloads=[raw_payload])
        return snapshot

    async def save_snapshots(
        self,
        snapshots: Iterable[OrderbookSnapshot],
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        snapshots = tuple(snapshots)
        payloads = raw_payloads or (None,) * len(snapshots)
        rows = [
            _row_dict(
                OrderbookSnapshotModel.from_domain(
                    snapshot,
                    trace_id=trace_id,
                    source=source,
                    raw_payload=payload,
                )
            )
            for snapshot, payload in zip(snapshots, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            OrderbookSnapshotModel,
            rows,
            conflict_columns=("snapshot_key",),
            update_columns=(
                "trace_id",
                "source",
                "token_id",
                "condition_id",
                "market_slug",
                "received_at",
                "best_bid",
                "best_ask",
                "best_bid_size",
                "best_ask_size",
                "last_trade_price",
                "tick_size",
                "bids",
                "asks",
                "raw_payload",
                "updated_at",
            ),
        )

    async def list_latest_snapshot(
        self,
        token_id: str,
    ) -> OrderbookSnapshot | None:
        stmt = (
            select(OrderbookSnapshotModel)
            .where(OrderbookSnapshotModel.token_id == token_id)
            .order_by(OrderbookSnapshotModel.received_at.desc(), OrderbookSnapshotModel.id.desc())
            .limit(1)
        )
        row = await self._session.scalar(stmt)
        return None if row is None else row.to_domain()

    async def list_snapshots(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        token_id: str | None = None,
        condition_id: str | None = None,
    ) -> RepositoryPage[OrderbookSnapshot]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(OrderbookSnapshotModel).order_by(
            OrderbookSnapshotModel.received_at.desc(),
            OrderbookSnapshotModel.id.desc(),
        )
        if token_id is not None:
            stmt = stmt.where(OrderbookSnapshotModel.token_id == token_id)
        if condition_id is not None:
            stmt = stmt.where(OrderbookSnapshotModel.condition_id == condition_id)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
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
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)


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
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)


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
    ) -> RepositoryPage[Position]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(PositionModel).order_by(PositionModel.updated_at.desc(), PositionModel.id.desc())
        if condition_id is not None:
            stmt = stmt.where(PositionModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(PositionModel.token_id == token_id)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)


class AccountSnapshotRepository(BaseRepository):
    """账户余额快照仓储。"""

    async def save_snapshot(
        self,
        snapshot: AccountSnapshot,
        *,
        trace_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
        account_key: str = "primary",
    ) -> AccountSnapshot:
        await self.save_snapshots(
            [snapshot],
            trace_id=trace_id,
            raw_payloads=[raw_payload],
            account_key=account_key,
        )
        return snapshot

    async def save_snapshots(
        self,
        snapshots: Iterable[AccountSnapshot],
        *,
        trace_id: str | None = None,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
        account_key: str = "primary",
    ) -> int:
        snapshots = tuple(snapshots)
        payloads = raw_payloads or (None,) * len(snapshots)
        rows = [
            _row_dict(
                AccountSnapshotModel.from_domain(
                    snapshot,
                    trace_id=trace_id,
                    raw_payload=payload,
                    account_key=account_key,
                )
            )
            for snapshot, payload in zip(snapshots, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            AccountSnapshotModel,
            rows,
            conflict_columns=("account_key",),
            update_columns=(
                "trace_id",
                "balance_usdc",
                "allowance_usdc",
                "user_ws_connected",
                "allow_new_entries",
                "market_pauses",
                "last_reconcile_at",
                "raw_payload",
                "updated_at",
            ),
        )

    async def get_current_snapshot(self, *, account_key: str = "primary") -> AccountSnapshot | None:
        row = await self._session.scalar(
            select(AccountSnapshotModel).where(AccountSnapshotModel.account_key == account_key)
        )
        return None if row is None else row.to_domain()


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
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)


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
