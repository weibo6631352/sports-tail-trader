"""AdminService 查询方法 mixin。

这里集中所有"读快照 / 列表查询 / 投影"类方法。每个方法假定宿主类（AdminService）
提供了 ``self.runtime`` 字段以及 ``_serializer``、``_runtime_view``、
``_registry_snapshot``、``_account_snapshot``、``_with_repositories``、
``_slice_sequence``、``_market_ws_snapshot``、``_build_entry_plan_for_admin``、
``_candidate_payload``、``_candidate_source_markets``、``_entry_metadata_store``、
``_has_db_session_factory``、``_settings_value`` 等私有 helper。

不在 mixin 里声明 ``__slots__`` —— 让宿主 dataclass 决定布局。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_trader.app.admin_serialization import decimal_text, jsonable, page_payload
from polymarket_trader.app.admin_service_helpers import (
    MarketFeeSortField,
    SortDirection,
    _RepositoryGroup,
    _candidate_matches_filters,
    _live_source_gap_market_payload,
    _live_source_gap_outside_diagnostic_window,
    _live_source_gap_scope_markets,
    _live_source_gap_urgency,
    _live_source_gap_urgency_rank,
    _market_matches_fee_filters,
    _market_slug_prefix,
    _orderbook_has_no_quotes,
    _sort_markets,
)
from polymarket_trader.app.trade_replay import TradeReplayFilters, build_trade_replay_records
from polymarket_trader.domain.market import Market
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.infra.polymarket import PolymarketClientError


class AdminQueryMixin:
    """只读管理查询。运行时状态全部通过宿主 AdminService 的 self.runtime 与私有 helper 读取。"""

    runtime: Any | None  # 宿主声明真正的字段；这里只是给类型检查器看

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
        # 已完成权威账户同步后，空持仓本身就是当前交易事实；DB 只保留审计/恢复参考，
        # 不能在热状态为空时把旧快照重新投影成“当前持仓”。
        if snapshot.positions or snapshot.last_reconcile_at is not None or not self._has_db_session_factory():
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
        if snapshot is None or _orderbook_has_no_quotes(snapshot):
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

    def dump_decision_records(self) -> dict[str, Any]:
        """暴露当前进程 ``InMemoryDecisionRecorder`` 最近决策。

        给 ``polymarket_trader.tools.replay_decisions dump-recorder`` CLI 抓取。
        """

        recorder = getattr(self.runtime, "decision_recorder", None)
        if recorder is None:
            return {"records": []}
        snapshot = recorder.snapshot()
        records = []
        for record in snapshot:
            records.append(
                {
                    "hook_name": record.hook_name,
                    "trace_id": record.trace_id,
                    "recorded_at": record.recorded_at.isoformat(),
                    "condition_id": record.condition_id,
                    "token_id": record.token_id,
                    "market_slug": record.market_slug,
                    "context_payload": dict(record.context_payload),
                    "decision_payload": dict(record.decision_payload),
                    "extras": dict(record.extras),
                }
            )
        return {"records": records}


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
            else tuple(record for record in store.records() if bool(record.live_state_payload))
        )
        page = self._slice_sequence(records, limit=limit, offset=offset)
        return page_payload(page, serializer=lambda record: record.as_payload())

    async def list_sports_live_source_gaps(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        prefix: str | None = None,
        include_future_schedule: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """诊断已跟踪市场中缺少体育直播状态的覆盖缺口。

        该接口只读取运行时 registry 和 metadata store，用于实盘观察直播源/
        匹配覆盖率，不触发 discovery、订阅或交易判断。
        """

        registry = getattr(self.runtime, "registry", None)
        store = self._entry_metadata_store()
        if registry is None:
            empty = page_payload(self._slice_sequence((), limit=limit, offset=offset), serializer=lambda item: item)
            empty.update(
                {
                    "tracked_markets": 0,
                    "live_state_markets": 0,
                    "missing_live_state_markets": 0,
                    "by_prefix": [],
                }
            )
            return empty

        all_markets = tuple(registry.snapshot().markets)
        markets = _live_source_gap_scope_markets(self.runtime, all_markets)
        scoped_condition_ids = {market.condition_id for market in markets}
        live_state_records = (
            ()
            if store is None
            else tuple(
                record
                for record in store.records()
                if bool(record.live_state_payload) and record.condition_id in scoped_condition_ids
            )
        )
        if now is None:
            now = datetime.now(timezone.utc)
        missing_markets = []
        deferred_future_schedule_count = 0
        normalized_prefix = None if prefix is None else prefix.strip().lower()
        for market in markets:
            record = None if store is None else store.find(
                condition_id=market.condition_id,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
            )
            if record is not None and bool(record.live_state_payload):
                continue
            market_prefix = _market_slug_prefix(market)
            if normalized_prefix and market_prefix != normalized_prefix:
                continue
            urgency = _live_source_gap_urgency(market, now=now)
            if _live_source_gap_outside_diagnostic_window(market, now=now):
                continue
            if urgency == "future_schedule" and not include_future_schedule:
                deferred_future_schedule_count += 1
                continue
            missing_markets.append(market)

        missing_markets = sorted(
            missing_markets,
            key=lambda market: (
                _live_source_gap_urgency_rank(_live_source_gap_urgency(market, now=now)),
                market.game_start_time or datetime.max.replace(tzinfo=timezone.utc),
                market.market_slug or market.condition_id,
            ),
        )
        by_prefix_counts: dict[str, int] = {}
        by_urgency_counts: dict[str, int] = {}
        for market in missing_markets:
            market_prefix = _market_slug_prefix(market)
            by_prefix_counts[market_prefix] = by_prefix_counts.get(market_prefix, 0) + 1
            urgency = _live_source_gap_urgency(market, now=now)
            by_urgency_counts[urgency] = by_urgency_counts.get(urgency, 0) + 1
        by_prefix = [
            {"prefix": item_prefix, "count": count}
            for item_prefix, count in sorted(
                by_prefix_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        by_urgency = [
            {"urgency": urgency, "count": count}
            for urgency, count in sorted(
                by_urgency_counts.items(),
                key=lambda item: (_live_source_gap_urgency_rank(item[0]), item[0]),
            )
        ]

        page = self._slice_sequence(tuple(missing_markets), limit=limit, offset=offset)
        payload = page_payload(
            page,
            serializer=lambda market: _live_source_gap_market_payload(market, now=now),
        )
        payload.update(
            {
                "tracked_markets": len(markets),
                "total_tracked_markets": len(all_markets),
                "live_state_markets": len(live_state_records),
                "missing_live_state_markets": len(missing_markets),
                "deferred_future_schedule_markets": deferred_future_schedule_count,
                "by_prefix": by_prefix,
                "by_urgency": by_urgency,
                "prefix": normalized_prefix,
                "include_future_schedule": include_future_schedule,
            }
        )
        return payload

    async def list_strategy_candidates(
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
                summary = plan.summary
                if summary is None or not summary.reason:
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


