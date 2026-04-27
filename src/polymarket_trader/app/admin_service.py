from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Literal, Mapping, Sequence, cast
from uuid import uuid4

from polymarket_trader.app.admin_operations import (
    normalize_condition_ids,
)
from polymarket_trader.app.admin_order_control import AdminOrderController
from polymarket_trader.app.admin_runtime_view import AdminRuntimeView
from polymarket_trader.app.admin_serialization import AdminSerializer, decimal_text, jsonable, page_payload
from polymarket_trader.app.order_projection import AccountStateProjector, normalize_order_id
from polymarket_trader.app.trade_replay import TradeReplayFilters, build_trade_replay_records
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order, OrderResultStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.workers.trading_decision_event_payloads import (
    TRADING_DECISION_WORKER_ORIGIN,
    serialize_allocation,
    serialize_allocation_plan,
    serialize_intent,
    serialize_plan_metadata,
    serialize_review,
    snapshot_allowance,
    snapshot_balance,
)
from polymarket_trader.infra.db import (
    AllocationRepository,
    AuditEventRepository,
    FillRepository,
    MarketRepository,
    OrderRepository,
    OutboxEventRepository,
    PositionRepository,
    RepositoryPage,
)
from polymarket_trader.infra.polymarket import PolymarketClientError
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.runtime.registry import MarketRegistrySnapshot


MarketFeeSortField = Literal[
    "market_slug",
    "fee_rate_bps",
    "fee_rate_updated_at",
    "maker_base_fee_bps",
    "taker_base_fee_bps",
]
SortDirection = Literal["asc", "desc"]


def _market_matches_fee_filters(
    market: Market,
    *,
    fees_enabled: bool | None = None,
    fee_rate_bps_min: int | None = None,
    fee_rate_bps_max: int | None = None,
    maker_base_fee_bps_min: int | None = None,
    maker_base_fee_bps_max: int | None = None,
    taker_base_fee_bps_min: int | None = None,
    taker_base_fee_bps_max: int | None = None,
) -> bool:
    if fees_enabled is not None and market.fees_enabled is not fees_enabled:
        return False
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


def _market_sort_value(market: Market, sort_by: MarketFeeSortField) -> object | None:
    return {
        "market_slug": market.market_slug,
        "fee_rate_bps": market.fee_rate_bps,
        "fee_rate_updated_at": market.fee_rate_updated_at,
        "maker_base_fee_bps": market.maker_base_fee_bps,
        "taker_base_fee_bps": market.taker_base_fee_bps,
    }[sort_by]


def _sort_markets(
    markets: Sequence[Market],
    *,
    sort_by: MarketFeeSortField | None = None,
    sort_direction: SortDirection = "desc",
) -> tuple[Market, ...]:
    if sort_by is None:
        return tuple(markets)
    present = [market for market in markets if _market_sort_value(market, sort_by) is not None]
    missing = [market for market in markets if _market_sort_value(market, sort_by) is None]
    present.sort(
        key=lambda market: cast(Any, _market_sort_value(market, sort_by)),
        reverse=sort_direction == "desc",
    )
    return tuple(present + missing)


def _candidate_matches_filters(
    candidate: Mapping[str, Any],
    *,
    market_type: str | None,
    game_status: str | None,
    action: str | None,
    execution_permission: str | None,
    accepted: bool | None,
    confirmable: bool | None,
    league: str | None,
) -> bool:
    """判断候选投影是否满足管理台筛选条件。"""

    if not _text_filter_matches(candidate.get("market_type"), market_type):
        return False
    if not _text_filter_matches(candidate.get("game_status"), game_status):
        return False
    if not _text_filter_matches(candidate.get("action"), action):
        return False
    if not _text_filter_matches(candidate.get("execution_permission"), execution_permission):
        return False
    if not _text_filter_matches(candidate.get("league"), league):
        return False
    if accepted is not None and bool(candidate.get("accepted")) is not accepted:
        return False
    if confirmable is not None and bool(candidate.get("confirmable")) is not confirmable:
        return False
    return True


def _text_filter_matches(value: object, expected: str | None) -> bool:
    if expected is None or not expected.strip():
        return True
    return str(value or "").strip().lower() == expected.strip().lower()


@dataclass(frozen=True, slots=True)
class AdminService:
    """Coordinates read-only admin queries and controlled manual operations."""

    runtime: Any | None = None

    def bind_runtime(self, runtime: Any) -> "AdminService":
        return AdminService(runtime=runtime)

    def health_snapshot(self) -> dict[str, Any]:
        return self._runtime_view().health_snapshot()

    def readiness_snapshot(self) -> dict[str, Any]:
        return self._runtime_view().readiness_snapshot()

    async def runtime_snapshot(self) -> dict[str, Any]:
        return await self._runtime_view().runtime_snapshot()

    async def list_markets(
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
        sort_by: MarketFeeSortField | None = None,
        sort_direction: SortDirection = "desc",
    ) -> dict[str, Any]:
        registry = self._registry_snapshot()
        if registry.markets or not self._has_db_session_factory():
            account = self._account_snapshot()
            markets = _sort_markets(
                tuple(
                    market
                    for market in registry.markets
                    if (trading_status is None or market.trading_status.value == trading_status)
                    and _market_matches_fee_filters(
                        market,
                        fees_enabled=fees_enabled,
                        fee_rate_bps_min=fee_rate_bps_min,
                        fee_rate_bps_max=fee_rate_bps_max,
                        maker_base_fee_bps_min=maker_base_fee_bps_min,
                        maker_base_fee_bps_max=maker_base_fee_bps_max,
                        taker_base_fee_bps_min=taker_base_fee_bps_min,
                        taker_base_fee_bps_max=taker_base_fee_bps_max,
                    )
                ),
                sort_by=sort_by,
                sort_direction=sort_direction,
            )
            page = self._slice_sequence(markets, limit=limit, offset=offset)
            items = [
                self._serializer().market_view(
                    market,
                    account_snapshot=account,
                    registry_snapshot=registry,
                )
                for market in page.items
            ]
            return page_payload(
                RepositoryPage(items=tuple(items), total=len(markets), limit=page.limit, offset=page.offset),
                serializer=lambda item: item,
            )

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.market.list_markets_snapshot(
                limit=limit,
                offset=offset,
                trading_status=trading_status,
                fees_enabled=fees_enabled,
                fee_rate_bps_min=fee_rate_bps_min,
                fee_rate_bps_max=fee_rate_bps_max,
                maker_base_fee_bps_min=maker_base_fee_bps_min,
                maker_base_fee_bps_max=maker_base_fee_bps_max,
                taker_base_fee_bps_min=taker_base_fee_bps_min,
                taker_base_fee_bps_max=taker_base_fee_bps_max,
                sort_by=sort_by,
                sort_direction=sort_direction,
            )

        page = await self._with_repositories(_query)
        registry = self._registry_snapshot()
        account = self._account_snapshot()
        items = [
            self._serializer().market_view(
                market,
                account_snapshot=account,
                registry_snapshot=registry,
            )
            for market in page.items
        ]
        return page_payload(
            RepositoryPage(items=tuple(items), total=page.total, limit=page.limit, offset=page.offset),
            serializer=lambda item: item,
        )

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
            ]
            page = self._slice_sequence(orders, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().order)

        if not self._has_db_session_factory():
            snapshot = self._account_snapshot()
            orders = [
                order
                for order in snapshot.open_orders
                if (condition_id is None or order.condition_id == condition_id)
                and (token_id is None or order.token_id == token_id)
                and (trace_id is None or order.trace_id == trace_id)
                and (order_id is None or order.order_id == order_id)
                and (trade_id is None or order.trade_id == trade_id)
            ]
            page = self._slice_sequence(orders, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().order)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.order.list_orders_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                order_id=order_id,
                trade_id=trade_id,
                condition_id=condition_id,
                token_id=token_id,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=self._serializer().order)

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
    ) -> dict[str, Any]:
        if not self._has_db_session_factory():
            snapshot = self._account_snapshot()
            fills = [
                fill
                for fill in snapshot.fills
                if (trace_id is None or fill.trace_id == trace_id)
                and (order_id is None or fill.order_id == order_id)
                and (trade_id is None or fill.trade_id == trade_id)
                and (condition_id is None or fill.condition_id == condition_id)
                and (token_id is None or fill.token_id == token_id)
            ]
            page = self._slice_sequence(fills, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().fill)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.fill.list_fills_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                order_id=order_id,
                trade_id=trade_id,
                condition_id=condition_id,
                token_id=token_id,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=self._serializer().fill)

    async def list_positions(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self._account_snapshot()
        if snapshot.positions or not self._has_db_session_factory():
            positions = [
                position
                for position in snapshot.positions
                if (condition_id is None or position.condition_id == condition_id)
                and (token_id is None or position.token_id == token_id)
            ]
            page = self._slice_sequence(positions, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().position)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.position.list_positions_snapshot(
                limit=limit,
                offset=offset,
                condition_id=condition_id,
                token_id=token_id,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=self._serializer().position)

    async def get_market(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any] | None:
        market = self._resolve_market(
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
        )
        if market is None and self._has_db_session_factory():
            async def _query(repos: _RepositoryGroup) -> Market | None:
                if condition_id is not None:
                    market_by_condition = await repos.market.get_by_condition_id(condition_id)
                    if market_by_condition is not None:
                        return market_by_condition
                if token_id is not None:
                    market_by_token = await repos.market.get_by_token_id(token_id)
                    if market_by_token is not None:
                        return market_by_token
                if market_slug is not None:
                    return await repos.market.get_by_market_slug(market_slug)
                return None

            market = await self._with_repositories(_query)
        if market is None:
            return None
        return self._serializer().market_view(market)

    async def get_market_orderbook(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any] | None:
        market = self._resolve_market(
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
        )
        resolved_market_slug = market.market_slug if market is not None else market_slug
        resolved_condition_id = market.condition_id if market is not None else condition_id
        resolved_token_id = token_id
        if resolved_token_id is None:
            return None

        snapshot = self._market_ws_snapshot(resolved_token_id)
        source = "hot"
        if snapshot is None:
            orderbook = await self._clob_client().get_orderbook(
                resolved_token_id,
                market_slug=resolved_market_slug,
                condition_id=resolved_condition_id,
            )
            snapshot = orderbook.to_snapshot()
            source = "rest"
        return self._serializer().market_orderbook(
            token_id=resolved_token_id,
            condition_id=resolved_condition_id,
            market_slug=resolved_market_slug,
            orderbook=snapshot,
            source=source,
        )

    async def get_market_midpoint(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any] | None:
        market = self._resolve_market(
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
        )
        resolved_market_slug = market.market_slug if market is not None else market_slug
        resolved_condition_id = market.condition_id if market is not None else condition_id
        resolved_token_id = token_id
        if resolved_token_id is None:
            return None

        snapshot = self._market_ws_snapshot(resolved_token_id)
        source = "hot"
        if snapshot is not None and snapshot.best_bid is not None and snapshot.best_ask is not None:
            midpoint = (snapshot.best_bid + snapshot.best_ask) / Decimal("2")
        else:
            try:
                midpoint = await self._clob_client().get_midpoint(resolved_token_id)
            except PolymarketClientError as exc:
                if exc.status_code != 404:
                    raise
                midpoint = None
            source = "rest"
        return self._serializer().market_midpoint(
            token_id=resolved_token_id,
            condition_id=resolved_condition_id,
            market_slug=resolved_market_slug,
            midpoint=midpoint,
            orderbook=snapshot,
            source=source,
        )

    async def get_market_prices_history(
        self,
        *,
        token_id: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        interval: str | None = None,
        fidelity: int | None = None,
    ) -> dict[str, Any]:
        history = await self._clob_client().get_prices_history(
            token_id,
            start_ts=start_ts,
            end_ts=end_ts,
            interval=interval,
            fidelity=fidelity,
        )
        return self._serializer().market_prices_history(
            token_id=token_id,
            history=history,
            interval=interval,
            fidelity=fidelity,
        )

    async def list_audit_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        event_title: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any]:
        if not self._has_db_session_factory():
            page: RepositoryPage[Any] = RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().audit_event)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.audit.list_audit_events_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                event_title=event_title,
                condition_id=condition_id,
                token_id=token_id,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=self._serializer().audit_event)

    async def list_trade_replays(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        condition_id: str | None = None,
        token_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """聚合成交、持仓、审计和策略 metadata，返回只读复盘视图。"""

        filters = TradeReplayFilters(
            condition_id=condition_id,
            token_id=token_id,
            trace_id=trace_id,
        )
        if not self._has_db_session_factory():
            account = self._account_snapshot()
            records = build_trade_replay_records(
                markets=self._registry_snapshot().markets,
                orders=account.open_orders,
                fills=account.fills,
                positions=account.positions,
                audit_events=(),
                serializer=self._serializer(),
                filters=filters,
            )
            page = self._slice_sequence(records, limit=limit, offset=offset)
            return page_payload(page, serializer=lambda item: item)

        async def _query(repos: _RepositoryGroup) -> dict[str, Any]:
            query_limit = max(500, limit + offset)
            if condition_id is not None:
                market = await repos.market.get_by_condition_id(condition_id)
                markets = () if market is None else (market,)
            else:
                market_page = await repos.market.list_markets_snapshot(limit=query_limit, offset=0)
                markets = market_page.items
            order_page = await repos.order.list_orders_snapshot(
                limit=query_limit,
                offset=0,
                trace_id=trace_id,
                condition_id=condition_id,
                token_id=token_id,
            )
            fill_page = await repos.fill.list_fills_snapshot(
                limit=query_limit,
                offset=0,
                trace_id=trace_id,
                condition_id=condition_id,
                token_id=token_id,
            )
            position_page = await repos.position.list_positions_snapshot(
                limit=query_limit,
                offset=0,
                condition_id=condition_id,
                token_id=token_id,
            )
            audit_page = await repos.audit.list_audit_events_snapshot(
                limit=query_limit,
                offset=0,
                trace_id=trace_id,
                condition_id=condition_id,
                token_id=token_id,
            )
            records = build_trade_replay_records(
                markets=markets,
                orders=order_page.items,
                fills=fill_page.items,
                positions=position_page.items,
                audit_events=audit_page.items,
                serializer=self._serializer(),
                filters=filters,
            )
            page = self._slice_sequence(records, limit=limit, offset=offset)
            return page_payload(page, serializer=lambda item: item)

        return await self._with_repositories(_query)

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
        if not self._has_db_session_factory():
            page: RepositoryPage[Any] = RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().allocation)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.allocation.list_allocations_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                condition_id=condition_id,
                token_id=token_id,
                market_slug=market_slug,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=self._serializer().allocation)

    async def list_outbox_pending(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        runtime_outbox = getattr(self.runtime, "outbox", None)
        if runtime_outbox is not None and hasattr(runtime_outbox, "pending_events"):
            events = runtime_outbox.pending_events()
            if trace_id is not None:
                events = tuple(event for event in events if event.trace_id == trace_id)
            page = RepositoryPage(
                items=tuple(events[offset : offset + limit]),
                total=len(events),
                limit=limit,
                offset=offset,
            )
            return page_payload(page, serializer=self._serializer().outbox_event)

        page = RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
        return page_payload(page, serializer=self._serializer().outbox_event)

    async def portfolio_snapshot(self) -> dict[str, Any]:
        account = self._account_snapshot()
        registry = self._registry_snapshot()
        if not self._has_db_session_factory():
            return {
                "balance_usdc": decimal_text(account.balance_usdc),
                "allowance_usdc": decimal_text(account.allowance_usdc),
                "available_usdc": decimal_text(account.available_usdc),
                "position_count": len(account.positions),
                "open_order_count": len(account.open_orders),
                "fill_count": len(account.fills),
                "pause_count": len(account.market_pauses),
                "last_reconcile_at": jsonable(account.last_reconcile_at),
                "user_ws_connected": account.user_ws_connected,
                "allow_new_entries": account.allow_new_entries,
                "markets_tracked": len(registry.markets),
                "recent_allocations": [],
            }

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.allocation.list_allocations_snapshot(limit=50, offset=0)

        allocations = await self._with_repositories(_query)
        return {
            "balance_usdc": decimal_text(account.balance_usdc),
            "allowance_usdc": decimal_text(account.allowance_usdc),
            "available_usdc": decimal_text(account.available_usdc),
            "position_count": len(account.positions),
            "open_order_count": len(account.open_orders),
            "fill_count": len(account.fills),
            "pause_count": len(account.market_pauses),
            "last_reconcile_at": jsonable(account.last_reconcile_at),
            "user_ws_connected": account.user_ws_connected,
            "allow_new_entries": account.allow_new_entries,
            "markets_tracked": len(registry.markets),
            "recent_allocations": [
                self._serializer().allocation(allocation) for allocation in allocations.items
            ],
        }

    def workers_snapshot(self) -> dict[str, Any]:
        return self._runtime_view().workers_snapshot()

    def metrics_snapshot(self) -> dict[str, Any]:
        return self._runtime_view().metrics_snapshot()

    async def reconcile(
        self,
        *,
        trace_id: str | None = None,
        condition_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        reconcile_worker = getattr(self.runtime, "reconcile_worker", None)
        if reconcile_worker is None:
            return {
                "status": "failed",
                "reason": "reconcile_worker_unavailable",
                "trace_id": trace_id or uuid4().hex,
            }
        condition_id_filter = normalize_condition_ids(condition_ids)
        result = await reconcile_worker.reconcile_once(
            trace_id=trace_id,
            condition_ids=condition_id_filter or None,
        )
        return self._serializer().reconcile_result(result)

    async def replace_order(
        self,
        *,
        order_id: str,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        new_price: Decimal,
        size_shares: Decimal | None = None,
        operator: str = "manual",
        reason: str = "admin_replace_order",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._order_controller().replace_order(
            order_id=order_id,
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
            new_price=new_price,
            size_shares=size_shares,
            operator=operator,
            reason=reason,
            trace_id=trace_id,
        )

    async def upsert_sports_live_state(
        self,
        *,
        sports_tail_game: Mapping[str, Any],
        condition_id: str | None = None,
        market_slug: str | None = None,
        event_slug: str | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        """写入体育直播状态 metadata，不触发交易判断。"""

        store = self._entry_metadata_store()
        if store is None:
            return {"status": "failed", "reason": "entry_metadata_store_unavailable"}
        record = store.upsert(
            condition_id=condition_id,
            market_slug=market_slug,
            event_slug=event_slug,
            source=source,
            metadata={"sports_tail_game": dict(sports_tail_game)},
        )
        return {"status": "ok", "record": record.as_payload()}

    async def list_sports_live_states(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页返回当前运行时保存的体育直播状态 metadata。"""

        store = self._entry_metadata_store()
        records = (
            ()
            if store is None
            else tuple(record for record in store.records() if "sports_tail_game" in record.metadata)
        )
        page = self._slice_sequence(records, limit=limit, offset=offset)
        return page_payload(page, serializer=lambda record: record.as_payload())

    async def list_sports_tail_candidates(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
        market_type: str | None = None,
        game_status: str | None = None,
        action: str | None = None,
        execution_permission: str | None = None,
        accepted: bool | None = None,
        confirmable: bool | None = None,
        league: str | None = None,
    ) -> dict[str, Any]:
        """从热态 market、orderbook 与直播 metadata 投影体育扫尾候选。"""

        candidates: list[dict[str, Any]] = []
        account = self._account_snapshot()
        source_markets = self._candidate_source_markets(
            condition_id=condition_id,
            token_id=token_id,
            market_slug=market_slug,
        )
        for index, market in enumerate(source_markets, start=1):
            if index % 20 == 0:
                await asyncio.sleep(0)
            for outcome in market.outcomes:
                if token_id is not None and outcome.token_id != token_id:
                    continue
                orderbook = self._market_ws_snapshot(outcome.token_id)
                if orderbook is None:
                    continue
                plan = self._build_entry_plan_for_admin(
                    market=market,
                    token_id=outcome.token_id,
                    orderbook=orderbook,
                    account=account,
                )
                metadata = dict(plan.metadata or {})
                if "sports_tail_reason" not in metadata:
                    continue
                candidate = self._candidate_payload(market, outcome.token_id, plan)
                if not _candidate_matches_filters(
                    candidate,
                    market_type=market_type,
                    game_status=game_status,
                    action=action,
                    execution_permission=execution_permission,
                    accepted=accepted,
                    confirmable=confirmable,
                    league=league,
                ):
                    continue
                candidates.append(candidate)
        page = self._slice_sequence(candidates, limit=limit, offset=offset)
        payload = page_payload(page, serializer=lambda item: item)
        payload["has_more"] = offset + len(page.items) < page.total
        payload["source_markets"] = len(source_markets)
        return payload

    async def confirm_sports_tail_candidate(
        self,
        *,
        condition_id: str | None = None,
        token_id: str,
        market_slug: str | None = None,
        operator: str = "manual",
        note: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """人工确认体育扫尾候选，并经交易服务和风控提交。"""

        trace_id = trace_id or uuid4().hex
        market = self._resolve_market(
            condition_id=condition_id,
            market_slug=market_slug,
            token_id=token_id,
        )
        if market is None:
            return {"status": "failed", "trace_id": trace_id, "reason": "market_not_found"}
        if token_id not in market.token_ids:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "token_not_found",
                "market": self._serializer().market(market),
            }
        orderbook = self._market_ws_snapshot(token_id)
        if orderbook is None:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "orderbook_unavailable",
                "market": self._serializer().market(market),
            }

        account = self._account_snapshot()
        metadata = self._entry_metadata_for_market(market)
        candidate_plan = self._build_entry_plan_for_admin(
            market=market,
            token_id=token_id,
            orderbook=orderbook,
            account=account,
            trace_id=trace_id,
            metadata=metadata,
        )
        candidate = self._candidate_payload(market, token_id, candidate_plan)
        if not candidate["confirmable"]:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "candidate_not_confirmable",
                "candidate": candidate,
            }

        metadata.update(
            {
                "sports_tail_manual_confirmed": True,
                "sports_tail_confirmed_by": operator,
                "sports_tail_confirm_reason": note or "manual_confirm",
            }
        )
        plan = self._build_entry_plan_for_admin(
            market=market,
            token_id=token_id,
            orderbook=orderbook,
            account=account,
            trace_id=trace_id,
            metadata=metadata,
        )
        if not plan.ready_to_trade or plan.intent is None:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": plan.reason or "entry_plan_not_ready",
                "candidate": self._candidate_payload(market, token_id, plan),
            }

        review = await self._trading_service().review_intent(
            plan.intent,
            market=market,
            orderbook=orderbook,
            position=account.get_position(market.condition_id, token_id),
            open_orders=account.open_orders_for_market(market.condition_id, token_id),
            allocation_plan=plan.allocation_plan,
            classification_passed=True,
            balance_usdc=snapshot_balance(account),
            allowance_usdc=snapshot_allowance(account),
            max_order_usdc=self._settings_value("max_order_usdc"),
            max_market_usdc=self._settings_value("max_market_usdc"),
            max_total_usdc=self._settings_value("max_total_usdc"),
            max_open_orders=self._settings_value("max_open_orders"),
            order_retry_limit=self._settings_value("order_retry_limit"),
            operation="admin_confirm_entry",
        )
        self._project_manual_entry_result(review, snapshot=account)
        await self._publish_candidate_confirmation_review(market=market, plan=plan, review=review)
        order_result = review.order_result
        failed = (
            order_result is None
            or order_result.status in {OrderResultStatus.FAILED, OrderResultStatus.REJECTED}
        )
        return {
            "status": "failed" if failed else "ok",
            "trace_id": trace_id,
            "reason": (
                review.risk_decision.reason
                if review.risk_decision is not None and not review.risk_decision.passed
                else (order_result.reason if order_result is not None else "")
            ),
            "candidate": self._candidate_payload(market, token_id, plan),
            "review": self._serializer().review(review),
        }

    def _serializer(self) -> AdminSerializer:
        return AdminSerializer(
            account_snapshot_provider=self._account_snapshot,
            registry_snapshot_provider=self._registry_snapshot,
            market_ws_snapshot=self._market_ws_snapshot,
        )

    def _runtime_view(self) -> AdminRuntimeView:
        return AdminRuntimeView(runtime=self.runtime)

    def _order_controller(self) -> AdminOrderController:
        return AdminOrderController(
            runtime=self.runtime,
            serializer=self._serializer(),
            account_snapshot=self._account_snapshot,
            resolve_market=self._resolve_market,
            trading_service=self._trading_service,
            find_open_order=self._find_open_order,
        )

    def _build_entry_plan_for_admin(
        self,
        *,
        market: Market,
        token_id: str,
        orderbook: OrderbookSnapshot,
        account: AccountSnapshot,
        trace_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ):
        settings = getattr(self.runtime, "settings", None)
        return self._trading_decision_service().build_entry_plan(
            market=market,
            orderbook=orderbook,
            # 候选投影和人工确认属于受控操作入口，不使用自动入场开关截断候选生成；
            # 仓位、挂单、余额仍显式传入，并在确认提交前继续经过 RiskManager。
            account_snapshot=None,
            token_id=token_id,
            trace_id=trace_id,
            portfolio_budget_usdc=getattr(settings, "portfolio_budget_usdc", Decimal("0")),
            available_usdc=account.available_usdc,
            max_order_usdc=getattr(settings, "max_order_usdc", Decimal("0")),
            max_market_usdc=getattr(settings, "max_market_usdc", Decimal("0")),
            max_total_usdc=getattr(settings, "max_total_usdc", Decimal("0")),
            positions=account.positions,
            open_orders=account.open_orders,
            metadata=metadata if metadata is not None else self._entry_metadata_for_market(market),
        )

    def _candidate_payload(self, market: Market, token_id: str, plan) -> dict[str, Any]:
        metadata = dict(plan.metadata or {})
        sports_tail_game = metadata.get("sports_tail_game") if isinstance(metadata.get("sports_tail_game"), Mapping) else {}
        outcome = market.get_outcome_by_token_id(token_id)
        action = str(metadata.get("sports_tail_action") or "")
        execution_permission = metadata.get("sports_execution_permission")
        return {
            "candidate_id": f"{plan.trace_id}:{market.condition_id}:{token_id}",
            "trace_id": plan.trace_id,
            "condition_id": market.condition_id,
            "market_slug": market.market_slug,
            "event_slug": market.event_slug,
            "event_title": market.event_title,
            "token_id": token_id,
            "outcome": None if outcome is None else outcome.outcome,
            "ready_to_trade": plan.ready_to_trade,
            "accepted": bool(action and action != "reject"),
            "confirmable": (
                execution_permission == "manual_confirm"
                and action == "manual_confirm"
                and not bool(metadata.get("sports_tail_manual_confirmed"))
            ),
            "reason": str(metadata.get("sports_tail_reason") or plan.reason or ""),
            "action": action,
            "execution_permission": execution_permission,
            "league": sports_tail_game.get("league") or metadata.get("sports_league"),
            "home_name": sports_tail_game.get("home_name"),
            "away_name": sports_tail_game.get("away_name"),
            "period": sports_tail_game.get("period"),
            "observed_at": sports_tail_game.get("observed_at"),
            "market_type": metadata.get("market_type"),
            "side": metadata.get("side"),
            "line": metadata.get("line"),
            "best_ask": metadata.get("best_ask"),
            "total_score": metadata.get("total_score"),
            "seconds_remaining": metadata.get("seconds_remaining"),
            "game_status": metadata.get("game_status"),
            "sports_risk_reason": metadata.get("sports_risk_reason"),
            "exit_plan": metadata.get("sports_exit_plan"),
            "allocation": None if plan.allocation is None else {
                "target_budget_usdc": decimal_text(plan.allocation.target_budget_usdc),
                "buy_budget_usdc": decimal_text(plan.allocation.buy_budget_usdc),
                "reason": plan.allocation.reason,
                "release_reason": plan.allocation.release_reason,
            },
            "intent": None if plan.intent is None else serialize_intent(plan.intent),
            "payload": jsonable(metadata),
        }

    def _entry_metadata_for_market(self, market: Market) -> dict[str, Any]:
        store = self._entry_metadata_store()
        if store is None:
            return {}
        return store.metadata_for(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
        )

    def _candidate_source_markets(
        self,
        *,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> tuple[Market, ...]:
        """候选展示只读取已形成运行时事实的市场，不用页面请求触发全量业务评估。"""

        if condition_id is not None or token_id is not None or market_slug is not None:
            market = self._resolve_market(
                condition_id=condition_id,
                token_id=token_id,
                market_slug=market_slug,
            )
            return () if market is None else (market,)

        store = self._entry_metadata_store()
        registry = getattr(self.runtime, "registry", None)
        if store is None or registry is None:
            return ()

        markets: dict[str, Market] = {}
        for record in store.records():
            if "sports_tail_game" not in record.metadata:
                continue
            market = None
            if record.condition_id:
                market = registry.get_by_condition_id(record.condition_id)
            if market is None and record.market_slug:
                market = registry.get_by_slug(record.market_slug)
            if market is None and record.event_slug:
                market = registry.get_by_slug(record.event_slug)
            if market is not None:
                markets[market.condition_id] = market
        return tuple(markets.values())

    def _project_manual_entry_result(self, review, *, snapshot: AccountSnapshot) -> None:
        account_state = getattr(self.runtime, "account_state_store", None)
        if account_state is None or review.order_result is None:
            return
        projector = AccountStateProjector(account_state)
        projector.apply_buy_result(review.order_result, snapshot=snapshot)
        projector.apply_result_flags(review.order_result, snapshot=snapshot)

    async def _publish_candidate_confirmation_review(self, *, market: Market, plan, review) -> None:
        event_bus = getattr(self.runtime, "event_bus", None)
        if event_bus is None or plan.intent is None:
            return
        await event_bus.publish(
            OutboxPriority.P3,
            DomainEvent(
                trace_id=plan.trace_id,
                event_type=(
                    DomainEventType.RISK_CHECK_PASSED
                    if review.risk_decision is not None and review.risk_decision.passed
                    else DomainEventType.RISK_CHECK_FAILED
                ),
                event_id=uuid4().hex,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
                condition_id=market.condition_id,
                token_id=plan.intent.token_id,
                reason="" if review.risk_decision is None else review.risk_decision.reason,
                payload={
                    "origin": "admin_candidate_confirm",
                    "entry_origin": TRADING_DECISION_WORKER_ORIGIN,
                    "operator": jsonable((plan.metadata or {}).get("sports_tail_confirmed_by")),
                    "confirm_reason": jsonable((plan.metadata or {}).get("sports_tail_confirm_reason")),
                    "allocation_plan": serialize_allocation_plan(plan),
                    "allocation": serialize_allocation(plan),
                    "plan_metadata": serialize_plan_metadata(plan),
                    "intent": serialize_intent(plan.intent),
                    "review": serialize_review(review),
                },
            ),
        )

    def _account_snapshot(self) -> AccountSnapshot:
        account_state = getattr(self.runtime, "account_state_store", None)
        if account_state is not None:
            return account_state.snapshot()
        return AccountSnapshot()

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        registry = getattr(self.runtime, "registry", None)
        if registry is not None:
            return registry.snapshot()
        return MarketRegistrySnapshot(tuple())

    def _has_db_session_factory(self) -> bool:
        return getattr(self.runtime, "db_session_factory", None) is not None

    def _market_ws_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        worker = getattr(self.runtime, "market_ws_worker", None)
        if worker is None:
            return None
        snapshot = getattr(worker, "snapshot", None)
        if not callable(snapshot):
            return None
        return snapshot(token_id)

    def _clob_client(self) -> Any:
        clob_client = getattr(self.runtime, "clob_client", None)
        if clob_client is None:
            raise RuntimeError("clob_client unavailable")
        return clob_client

    def _trading_service(self) -> TradingService:
        trading_service = getattr(self.runtime, "trading_service", None)
        if trading_service is None:
            raise RuntimeError("trading_service unavailable")
        return trading_service

    def _trading_decision_service(self) -> TradingDecisionService:
        trading_decision_service = getattr(self.runtime, "trading_decision_service", None)
        if trading_decision_service is None:
            raise RuntimeError("trading_decision_service unavailable")
        return trading_decision_service

    def _entry_metadata_store(self) -> Any | None:
        return getattr(self.runtime, "entry_metadata_store", None)

    def _settings_value(self, name: str) -> Any:
        settings = getattr(self.runtime, "settings", None)
        return None if settings is None else getattr(settings, name, None)

    def _resolve_market(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Market | None:
        registry = getattr(self.runtime, "registry", None)
        if registry is not None:
            if condition_id is not None:
                market = registry.get_by_condition_id(condition_id)
                if market is not None:
                    return market
            if token_id is not None:
                market = registry.get_by_token_id(token_id)
                if market is not None:
                    return market
            if market_slug is not None:
                market = registry.get_by_slug(market_slug)
                if market is not None:
                    return market
        return None

    async def _with_repositories(self, callback: Callable[[_RepositoryGroup], Any]) -> Any:
        session_factory = getattr(self.runtime, "db_session_factory", None)
        if session_factory is None:
            raise RuntimeError("db_session_factory unavailable")
        async with session_factory() as session:
            repositories = _RepositoryGroup(
                audit=AuditEventRepository(session),
                market=MarketRepository(session),
                order=OrderRepository(session),
                fill=FillRepository(session),
                position=PositionRepository(session),
                allocation=AllocationRepository(session),
                outbox=OutboxEventRepository(session),
            )
            return await callback(repositories)

    def _slice_sequence(
        self,
        items: Sequence[Any],
        *,
        limit: int,
        offset: int,
    ) -> RepositoryPage[Any]:
        if limit <= 0:
            limit = 100
        if offset < 0:
            offset = 0
        sliced = tuple(items[offset : offset + limit])
        return RepositoryPage(items=sliced, total=len(items), limit=limit, offset=offset)

    def _find_open_order(
        self,
        snapshot: AccountSnapshot,
        *,
        order_id: str,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Order | None:
        matches = [
            order
            for order in snapshot.open_orders
            if order.open
            and order_id in {normalize_order_id(order), order.order_id, order.idempotency_key}
            and (market_slug is None or order.market_slug == market_slug)
            and (condition_id is None or order.condition_id == condition_id)
            and (token_id is None or order.token_id == token_id)
        ]
        if len(matches) != 1:
            return None
        return matches[0]

@dataclass(frozen=True, slots=True)
class _RepositoryGroup:
    audit: AuditEventRepository
    market: MarketRepository
    order: OrderRepository
    fill: FillRepository
    position: PositionRepository
    allocation: AllocationRepository
    outbox: OutboxEventRepository
