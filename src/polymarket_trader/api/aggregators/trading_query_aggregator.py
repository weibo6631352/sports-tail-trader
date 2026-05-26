"""TradingQueryAggregator —— orders / fills / positions / allocations 列表查询。

按 原架构方案 §12.2：
- list_positions / open_only=true 的 list_orders / list_fills 无 DB 时 →
  内存运营查询（来自 account_state_store snapshot）
- list_orders open_only=false / list_allocations → DB 审计查询

# Endpoint 对应

| Endpoint | 方法 |
|---|---|
| `GET /orders` | `list_orders(...)` |
| `GET /fills` | `list_fills(...)` |
| `GET /allocations` | `list_allocations(...)` |
| `GET /allocations/decisions` | `list_allocation_decisions(...)` |
| `GET /positions?limit=...` (legacy paged) | `list_positions(...)` |

注意：新版 `/positions` 已切到 PositionAggregator (DataGraph view); 本
aggregator 的 list_positions 仅用于 positions.py:positions_liquidity 内部
拉取分页持仓。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from polymarket_trader.serialization import page_payload
from polymarket_trader.api.serialization import ApiSerializer
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage

from ._db import RepositoryGroup, with_repositories
from ._helpers import slice_sequence
from .timeline_aggregator import TimelineAggregator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class TradingQueryAggregator:
    def __init__(
        self,
        *,
        runtime: Any = None,
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
        serializer: ApiSerializer | None = None,
    ) -> None:
        self._runtime = runtime
        self._session_factory = (
            session_factory
            if session_factory is not None
            else (getattr(runtime, "db_session_factory", None) if runtime else None)
        )
        self._serializer = serializer or ApiSerializer.from_runtime(runtime)
        self._timeline = TimelineAggregator(
            session_factory=self._session_factory,
            runtime=runtime,
            serializer=self._serializer,
        )

    def _account_snapshot(self) -> AccountSnapshot:
        store = getattr(self._runtime, "account_state_store", None) if self._runtime else None
        return store.snapshot() if store is not None else AccountSnapshot()

    async def list_orders(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        open_only: bool = True,
        condition_id: str | None = None,
        token_id: str | None = None,
        trace_id: str | None = None,
        order_id: str | None = None,
        trade_id: str | None = None,
        status: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        if open_only:
            snapshot = self._account_snapshot()
            orders = [
                order
                for order in snapshot.open_orders
                if (condition_id is None or order.condition_id == condition_id)
                and (token_id is None or order.token_id == token_id)
                and (trace_id is None or order.trace_id == trace_id)
                and (order_id is None or order.order_id == order_id)
                and (trade_id is None or order.trade_id == trade_id)
                and (status is None or (order.status is not None and order.status.value == status))
                and (time_range is None or time_range.contains(order.created_at))
            ]
            page = slice_sequence(orders, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer.order)

        if self._session_factory is None:
            snapshot = self._account_snapshot()
            orders = [
                order
                for order in snapshot.open_orders
                if (condition_id is None or order.condition_id == condition_id)
                and (token_id is None or order.token_id == token_id)
                and (trace_id is None or order.trace_id == trace_id)
                and (order_id is None or order.order_id == order_id)
                and (trade_id is None or order.trade_id == trade_id)
                and (status is None or (order.status is not None and order.status.value == status))
                and (time_range is None or time_range.contains(order.created_at))
            ]
            page = slice_sequence(orders, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer.order)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.order.list_orders_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                order_id=order_id,
                trade_id=trade_id,
                condition_id=condition_id,
                token_id=token_id,
                status=status,
                time_range=time_range,
            )

        page = await with_repositories(self._session_factory, _query)
        return page_payload(page, serializer=self._serializer.order)

    async def list_fills(
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
    ) -> dict[str, Any]:
        if self._session_factory is None:
            snapshot = self._account_snapshot()
            fills = [
                fill
                for fill in snapshot.fills
                if (trace_id is None or fill.trace_id == trace_id)
                and (order_id is None or fill.order_id == order_id)
                and (trade_id is None or fill.trade_id == trade_id)
                and (condition_id is None or fill.condition_id == condition_id)
                and (token_id is None or fill.token_id == token_id)
                and (time_range is None or time_range.contains(fill.created_at))
            ]
            page = slice_sequence(fills, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer.fill)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.fill.list_fills_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                order_id=order_id,
                trade_id=trade_id,
                condition_id=condition_id,
                token_id=token_id,
                time_range=time_range,
            )

        page = await with_repositories(self._session_factory, _query)
        return page_payload(page, serializer=self._serializer.fill)

    async def list_positions(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any]:
        """内存快照（启动竞态窗口返回空）。新版优先用 PositionAggregator。"""

        snapshot = self._account_snapshot()
        positions = [
            position
            for position in snapshot.positions
            if (condition_id is None or position.condition_id == condition_id)
            and (token_id is None or position.token_id == token_id)
        ]
        page = slice_sequence(positions, limit=limit, offset=offset)
        return page_payload(page, serializer=self._serializer.position)

    async def list_allocations(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> dict[str, Any]:
        if self._session_factory is None:
            empty: RepositoryPage[Any] = RepositoryPage(
                items=tuple(), total=0, limit=limit, offset=offset
            )
            return page_payload(empty, serializer=self._serializer.allocation)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.allocation.list_allocations_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                condition_id=condition_id,
                token_id=token_id,
                market_slug=market_slug,
            )

        page = await with_repositories(self._session_factory, _query)
        return page_payload(page, serializer=self._serializer.allocation)

    async def list_allocation_decisions(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """AllocationPlan 决策过程历史（基于 audit_events 投影）。"""

        return await self._timeline.list_audit_events(
            limit=limit,
            offset=offset,
            event_title=DomainEventType.ALLOCATION_DECISION_RECORDED.value,
            condition_id=condition_id,
            time_range=time_range,
        )
