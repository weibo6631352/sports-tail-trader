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
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.infra.polymarket import PolymarketClientError


_LATENCY_STAGES: tuple[tuple[str, str, str], ...] = (
    ("queue_to_sign", "queued_at", "signed_at"),
    ("sign_to_submit", "sign_started_at", "submitted_at"),
    ("submit_to_ack", "submitted_at", "ack_at"),
    ("queue_to_ack", "queued_at", "ack_at"),
)

_LATENCY_PERCENTILES: tuple[float, ...] = (0.5, 0.9, 0.95, 0.99)


def _parse_iso(ts: Any) -> datetime | None:
    if ts is None or not isinstance(ts, str):
        return None
    text = ts.strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _percentile(values: list[float], q: float) -> float | None:
    """线性插值的百分位数；空样本返回 None。

    避免引入 numpy 依赖；纯 Python 实现，按 PostgreSQL ``percentile_cont`` 同义。
    """

    if not values:
        return None
    if q <= 0:
        return values[0]
    if q >= 1:
        return values[-1]
    pos = q * (len(values) - 1)
    lower_idx = int(pos)
    frac = pos - lower_idx
    if lower_idx + 1 >= len(values):
        return values[lower_idx]
    return values[lower_idx] + frac * (values[lower_idx + 1] - values[lower_idx])


def _empty_latency_payload(
    event_types: tuple[str, ...],
    sample_limit: int,
    window_ms: int | None,
) -> dict[str, Any]:
    return {
        "window_ms": window_ms,
        "sample_limit": sample_limit,
        "event_types": list(event_types),
        "sample_count": 0,
        "stages": {
            stage_name: {"count": 0, "percentiles_ms": {}, "max_ms": None, "min_ms": None}
            for stage_name, _, _ in _LATENCY_STAGES
        },
    }


def _compute_latency_payload(
    events: tuple[Any, ...],
    event_types: tuple[str, ...],
    sample_limit: int,
    window_ms: int | None,
) -> dict[str, Any]:
    stages: dict[str, list[float]] = {name: [] for name, _, _ in _LATENCY_STAGES}
    for event in events:
        payload = event.payload or {}
        timestamps = payload.get("timestamps") if isinstance(payload, dict) else None
        if not isinstance(timestamps, dict):
            continue
        parsed: dict[str, datetime | None] = {
            key: _parse_iso(timestamps.get(key))
            for key in ("queued_at", "sign_started_at", "signed_at", "submitted_at", "ack_at")
        }
        for stage_name, start_key, end_key in _LATENCY_STAGES:
            start = parsed.get(start_key)
            end = parsed.get(end_key)
            if start is None or end is None:
                continue
            delta_ms = (end - start).total_seconds() * 1000.0
            if delta_ms < 0:
                continue
            stages[stage_name].append(delta_ms)

    stage_payload: dict[str, Any] = {}
    for stage_name in stages:
        values = sorted(stages[stage_name])
        percentile_map = {
            f"p{int(q * 100)}": _percentile(values, q) for q in _LATENCY_PERCENTILES
        }
        stage_payload[stage_name] = {
            "count": len(values),
            "percentiles_ms": percentile_map,
            "max_ms": values[-1] if values else None,
            "min_ms": values[0] if values else None,
        }
    return {
        "window_ms": window_ms,
        "sample_limit": sample_limit,
        "event_types": list(event_types),
        "sample_count": len(events),
        "stages": stage_payload,
    }


def _decision_record_payload(record: DecisionRecord) -> dict[str, Any]:
    """决策录制行的 admin 视图——保持字段命名与 DB 列对齐。"""

    return {
        "record_id": record.record_id,
        "trace_id": record.trace_id,
        "hook_name": record.hook_name or None,
        "condition_id": record.condition_id,
        "token_id": record.token_id,
        "market_slug": record.market_slug,
        "decision_input": dict(record.decision_input),
        "decision_output": dict(record.decision_output),
        "accepted": record.accepted,
        "reason": record.reason,
        "created_at": jsonable(record.created_at),
    }


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
        time_range: TimeRange | None = None,
        strategy_id: str | None = None,
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
                and (strategy_id is None or order.strategy_id == strategy_id)
                and (time_range is None or time_range.contains(order.created_at))
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
                and (strategy_id is None or order.strategy_id == strategy_id)
                and (time_range is None or time_range.contains(order.created_at))
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
                time_range=time_range,
                strategy_id=strategy_id,
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
        time_range: TimeRange | None = None,
        strategy_id: str | None = None,
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
                and (strategy_id is None or fill.strategy_id == strategy_id)
                and (time_range is None or time_range.contains(fill.created_at))
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
                time_range=time_range,
                strategy_id=strategy_id,
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
        strategy_id: str | None = None,
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
                and (strategy_id is None or position.strategy_id == strategy_id)
            ]
            page = self._slice_sequence(positions, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().position)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.position.list_positions_snapshot(
                limit=limit,
                offset=offset,
                condition_id=condition_id,
                token_id=token_id,
                strategy_id=strategy_id,
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
        time_range: TimeRange | None = None,
        strategy_id: str | None = None,
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
                time_range=time_range,
                strategy_id=strategy_id,
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
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """聚合成交、持仓、审计和策略 metadata，返回只读复盘视图。"""

        filters = TradeReplayFilters(
            condition_id=condition_id,
            token_id=token_id,
            trace_id=trace_id,
            strategy_id=strategy_id,
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
                strategy_id=strategy_id,
            )
            fill_page = await repos.fill.list_fills_snapshot(
                limit=query_limit,
                offset=0,
                trace_id=trace_id,
                condition_id=condition_id,
                token_id=token_id,
                strategy_id=strategy_id,
            )
            position_page = await repos.position.list_positions_snapshot(
                limit=query_limit,
                offset=0,
                condition_id=condition_id,
                token_id=token_id,
                strategy_id=strategy_id,
            )
            audit_page = await repos.audit.list_audit_events_snapshot(
                limit=query_limit,
                offset=0,
                trace_id=trace_id,
                condition_id=condition_id,
                token_id=token_id,
                strategy_id=strategy_id,
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
        strategy_id: str | None = None,
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
                strategy_id=strategy_id,
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

    async def list_outbox_failures(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        event_type: str | None = None,
        time_range: TimeRange | None = None,
        min_retry_count: int = 1,
    ) -> dict[str, Any]:
        """诊断持久化链路：当前重试中或带 ``last_error`` 的 outbox 事件。

        DB 是唯一真相来源；进程内存中的 ``pending_events`` 在运行时崩溃后会
        丢失，无法反映"上一次重启前失败的事件"。
        """

        if not self._has_db_session_factory():
            page: RepositoryPage[Any] = RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().outbox_event)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.outbox.list_failures_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                event_type=event_type,
                time_range=time_range,
                min_retry_count=min_retry_count,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=self._serializer().outbox_event)

    async def list_orderbook_history(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        token_id: str | None = None,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """历史盘口快照查询，按 ``received_at`` 倒序。

        ``orderbook_snapshots`` 表已经在落，本接口只暴露 GET。复盘"入场那一秒
        的盘口"用，按 token_id / condition_id + 时间窗过滤。
        """

        if not self._has_db_session_factory():
            page: RepositoryPage[Any] = RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().orderbook)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.orderbook.list_snapshots(
                limit=limit,
                offset=offset,
                token_id=token_id,
                condition_id=condition_id,
                time_range=time_range,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=self._serializer().orderbook)

    async def edge_realization_snapshot(
        self,
        *,
        limit: int = 200,
        strategy_id: str | None = None,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """Edge 实现度：预测 edge vs 实际 per-share 回报，按预测 edge 分桶聚合。

        只取 ``accepted=true`` 的决策（拒绝的没有 position 对应）；对每条决策
        从 ``decision_output`` 抽 ``fair_value`` 和 ``entry_price``，从 position 算
        ``realized_pnl / cost`` （已平仓优先）或 ``cash_pnl / cost`` （未平仓）。
        """

        from polymarket_trader.app.edge_realization import (
            aggregate_by_predicted_edge_buckets,
            build_edge_realization,
        )

        if not self._has_db_session_factory():
            return {
                "items": [],
                "buckets": list(aggregate_by_predicted_edge_buckets(())),
                "limit": limit,
            }

        async def _query(repos: _RepositoryGroup) -> tuple[Any, ...]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=limit,
                offset=0,
                accepted=True,
                condition_id=condition_id,
                strategy_id=strategy_id,
                time_range=time_range,
            )
            decisions = tuple(decision_page.items or ())
            keys = {(d.condition_id, d.token_id) for d in decisions if d.token_id}
            if not keys:
                return decisions, {}
            position_records: dict[tuple[str, str], Any] = {}
            condition_ids = {cid for cid, _ in keys}
            for cid in condition_ids:
                position_page = await repos.position.list_positions_snapshot(
                    limit=200,
                    offset=0,
                    condition_id=cid,
                    strategy_id=strategy_id,
                )
                for position in position_page.items or ():
                    position_records[(position.condition_id, position.token_id)] = position
            return decisions, position_records

        decisions, positions_by_key = await self._with_repositories(_query)
        items = build_edge_realization(
            decisions=decisions,
            positions_by_key=positions_by_key,
        )
        return {
            "items": [item.as_payload() for item in items],
            "buckets": list(aggregate_by_predicted_edge_buckets(items)),
            "limit": limit,
        }

    async def pnl_breakdown_snapshot(
        self,
        *,
        group_by: str,
        strategy_id: str | None = None,
        condition_id: str | None = None,
        position_limit: int = 5000,
    ) -> dict[str, Any]:
        """按维度分解的仓位 PnL 聚合。

        合法 ``group_by`` 由 ``pnl_breakdown.valid_group_by_values()`` 暴露：
        ``strategy_id`` / ``market_slug`` / ``condition_id`` / ``category`` /
        ``outcome`` / ``redeemable_status``。``category`` 和 ``outcome`` 维度
        额外做一次 markets 批量 join。
        """

        from polymarket_trader.app.pnl_breakdown import (
            aggregate_totals,
            build_pnl_breakdown,
            is_valid_group_by,
            valid_group_by_values,
        )

        if not is_valid_group_by(group_by):
            raise ValueError(
                f"unsupported group_by: {group_by} (valid: {valid_group_by_values()})"
            )

        if not self._has_db_session_factory():
            return {
                "group_by": group_by,
                "rows": [],
                "totals": aggregate_totals(()),
            }

        needs_markets = group_by in {"category", "outcome"}

        async def _query(repos: _RepositoryGroup) -> tuple[Any, ...]:
            position_page = await repos.position.list_positions_snapshot(
                limit=position_limit,
                offset=0,
                condition_id=condition_id,
                strategy_id=strategy_id,
            )
            positions = tuple(position_page.items or ())
            if not needs_markets or not positions:
                return positions, {}
            condition_ids = tuple({p.condition_id for p in positions if p.condition_id})
            markets = await repos.market.list_by_condition_ids(condition_ids)
            markets_by_condition = {m.condition_id: m for m in markets}
            return positions, markets_by_condition

        positions, markets_by_condition = await self._with_repositories(_query)
        rows = build_pnl_breakdown(
            positions=positions,
            markets_by_condition=markets_by_condition,
            group_by=group_by,
        )
        return {
            "group_by": group_by,
            "rows": [row.as_payload() for row in rows],
            "totals": aggregate_totals(rows),
        }

    async def get_trade_timeline(
        self,
        *,
        condition_id: str,
        token_id: str | None = None,
        time_range: TimeRange | None = None,
        limit: int = 1000,
        per_table_limit: int = 1000,
    ) -> dict[str, Any]:
        """单笔交易/单个市场全生命周期 timeline。

        合并 ``decision_records / orders / fills / audit_events / outbox_events``
        按时间戳升序，返回事件序列 + 当前 ``position`` 快照。每张表独立按
        ``per_table_limit`` 拉，最终统一按 ``limit`` 裁剪——避免长尾市场把响应体
        撑爆。
        """

        from polymarket_trader.app.trade_timeline import (
            TradeTimelineInputs,
            build_trade_timeline,
        )
        from polymarket_trader.domain.events import DomainEventType

        if not self._has_db_session_factory():
            return {
                "condition_id": condition_id,
                "token_id": token_id,
                "event_count": 0,
                "truncated": False,
                "events": [],
                "current_position": None,
            }

        reconcile_types = (
            DomainEventType.RECONCILE_DIFF_DETECTED.value,
            DomainEventType.RECONCILE_APPLIED.value,
            DomainEventType.RECONCILE_STARTED.value,
        )

        async def _query(repos: _RepositoryGroup) -> TradeTimelineInputs:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=per_table_limit,
                offset=0,
                condition_id=condition_id,
                time_range=time_range,
            )
            order_page = await repos.order.list_orders_snapshot(
                limit=per_table_limit,
                offset=0,
                condition_id=condition_id,
                token_id=token_id,
                time_range=time_range,
            )
            fill_page = await repos.fill.list_fills_snapshot(
                limit=per_table_limit,
                offset=0,
                condition_id=condition_id,
                token_id=token_id,
                time_range=time_range,
            )
            audit_page = await repos.audit.list_audit_events_snapshot(
                limit=per_table_limit,
                offset=0,
                condition_id=condition_id,
                token_id=token_id,
                time_range=time_range,
            )
            outbox_page = await repos.outbox.list_events_by_types_snapshot(
                event_types=reconcile_types,
                limit=per_table_limit,
                offset=0,
                condition_id=condition_id,
                time_range=time_range,
            )
            position_page = await repos.position.list_positions_snapshot(
                limit=per_table_limit,
                offset=0,
                condition_id=condition_id,
                token_id=token_id,
            )
            return TradeTimelineInputs(
                decisions=tuple(decision_page.items or ()),
                orders=tuple(order_page.items or ()),
                fills=tuple(fill_page.items or ()),
                audit_events=tuple(audit_page.items or ()),
                outbox_events=tuple(outbox_page.items or ()),
                positions=tuple(position_page.items or ()),
            )

        inputs = await self._with_repositories(_query)
        return build_trade_timeline(
            condition_id=condition_id,
            token_id=token_id,
            inputs=inputs,
            serializer=self._serializer(),
            limit=limit,
        )

    async def list_sports_live_events_history(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """历史体育实时事件——按 audit_events 中 ``event_title='sports_live_state_recorded'`` 过滤。

        每条事件 payload 含 score / clock / phase / signal_allowed / signal_reason
        等比赛快照，复盘"决策时的比分/时钟/赛况"必备。
        """

        from polymarket_trader.domain.events import DomainEventType

        return await self.list_audit_events(
            limit=limit,
            offset=offset,
            event_title=DomainEventType.SPORTS_LIVE_STATE_RECORDED.value,
            condition_id=condition_id,
            time_range=time_range,
        )

    async def list_allocation_decisions(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """AllocationPlan 决策过程历史。

        Payload 含 candidates / selected_condition_ids / skipped_reasons /
        budget——回答"为什么选这个市场、不选那个"。
        """

        from polymarket_trader.domain.events import DomainEventType

        return await self.list_audit_events(
            limit=limit,
            offset=offset,
            event_title=DomainEventType.ALLOCATION_DECISION_RECORDED.value,
            condition_id=condition_id,
            time_range=time_range,
        )

    async def list_risk_rejections(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """风控结构化拒绝详情——含完整 ``checks[]`` 投影。

        与 ``/analytics/rejections`` 的"reason 字符串 top 聚合"互补：这里要的是
        每次拒绝的逐条 check（``check_name / failed_field / value /
        suggested_action``），用于精确调整风控阈值。
        """

        from polymarket_trader.domain.events import DomainEventType

        return await self.list_audit_events(
            limit=limit,
            offset=offset,
            event_title=DomainEventType.RISK_REJECTION_RECORDED.value,
            condition_id=condition_id,
            time_range=time_range,
        )

    async def aggregate_risk_rejections(
        self,
        *,
        time_range: TimeRange | None = None,
        condition_id: str | None = None,
        sample_limit: int = 1000,
    ) -> dict[str, Any]:
        """按 ``check_name`` 聚合风控拒绝——回答"哪条风控规则在拒哪类市场"。"""

        from polymarket_trader.domain.events import DomainEventType

        if not self._has_db_session_factory():
            return {"buckets": [], "total_rejections": 0}

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.audit.list_audit_events_snapshot(
                limit=sample_limit,
                offset=0,
                event_title=DomainEventType.RISK_REJECTION_RECORDED.value,
                condition_id=condition_id,
                time_range=time_range,
            )

        page = await self._with_repositories(_query)
        from collections import Counter

        check_counter: Counter[str] = Counter()
        field_counter: Counter[str] = Counter()
        total = 0
        for event in (page.items or ()):
            payload = event.payload if isinstance(event.payload, dict) else {}
            total += 1
            checks = payload.get("checks")
            if isinstance(checks, list):
                for check in checks:
                    if not isinstance(check, dict):
                        continue
                    if check.get("passed") is True:
                        continue
                    name = str(check.get("name") or "(unnamed)")
                    field = str(check.get("field") or "(none)")
                    check_counter[name] += 1
                    field_counter[field] += 1
        return {
            "total_rejections": total,
            "by_check_name": [
                {"check_name": name, "count": cnt}
                for name, cnt in check_counter.most_common(50)
            ],
            "by_failed_field": [
                {"field": fld, "count": cnt}
                for fld, cnt in field_counter.most_common(50)
            ],
        }

    async def list_market_settlements(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """市场结算（settlement）历史——含 winning_token_id / outcome / 来源。"""

        from polymarket_trader.domain.events import DomainEventType

        return await self.list_audit_events(
            limit=limit,
            offset=offset,
            event_title=DomainEventType.MARKET_SETTLED.value,
            condition_id=condition_id,
            time_range=time_range,
        )

    async def aggregate_operator_interventions(
        self,
        *,
        operator: str | None = None,
        time_range: TimeRange | None = None,
        sample_limit: int = 2000,
    ) -> dict[str, Any]:
        """按 ``operator`` 聚合人工干预——audit_events.payload 里有 operator 字段。

        ``operator`` 缺省时返回所有 operator 的次数分布；指定后给该 operator
        最近 N 次干预的事件类型分布 + 时间线摘要。回答"谁在动盘、动了什么"。
        """

        from collections import Counter

        if not self._has_db_session_factory():
            return {
                "operator": operator,
                "total_events": 0,
                "by_operator": [],
                "by_event_title": [],
                "events": [],
            }

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.audit.list_audit_events_snapshot(
                limit=sample_limit,
                offset=0,
                time_range=time_range,
            )

        page = await self._with_repositories(_query)
        operator_counter: Counter[str] = Counter()
        event_counter: Counter[str] = Counter()
        events_sample: list[dict[str, Any]] = []
        total_with_operator = 0
        for event in (page.items or ()):
            payload = event.payload if isinstance(event.payload, dict) else {}
            op = payload.get("operator")
            if op is None:
                continue
            op_text = str(op)
            if operator is not None and op_text != operator:
                continue
            total_with_operator += 1
            operator_counter[op_text] += 1
            event_counter[event.event_title] += 1
            if len(events_sample) < 200:
                events_sample.append(
                    {
                        "event_id": event.event_id,
                        "event_title": event.event_title,
                        "operator": op_text,
                        "reason": event.reason,
                        "condition_id": event.condition_id,
                        "token_id": event.token_id,
                        "created_at": jsonable(event.created_at),
                    }
                )
        return {
            "operator": operator,
            "total_events": total_with_operator,
            "by_operator": [
                {"operator": op, "count": cnt} for op, cnt in operator_counter.most_common(50)
            ],
            "by_event_title": [
                {"event_title": title, "count": cnt}
                for title, cnt in event_counter.most_common(50)
            ],
            "events": events_sample,
        }

    async def run_parameter_sweep(
        self,
        *,
        candidates: dict[str, Any],
        per_decision_usdc: Decimal = Decimal("10"),
        strategy_id: str | None = None,
        time_range: TimeRange | None = None,
        decision_limit: int = 2000,
        settlement_limit: int = 2000,
    ) -> dict[str, Any]:
        """对历史决策回放给定参数候选笛卡尔积，输出每组 hypothetical PnL 排序。

        所有副作用都在内存（不改 ParameterStore、不下单），可在策略迭代期
        反复跑。``candidates`` 由调用层校验（白名单 + 笛卡尔积上限）。
        """

        from polymarket_trader.app.parameter_sweep import build_parameter_sweep
        from polymarket_trader.domain.events import DomainEventType

        if not self._has_db_session_factory():
            return build_parameter_sweep(
                decisions=(),
                settlements=(),
                candidates=candidates,
                per_decision_usdc=per_decision_usdc,
            )

        async def _query(repos: _RepositoryGroup) -> tuple[Any, Any]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=decision_limit,
                offset=0,
                strategy_id=strategy_id,
                time_range=time_range,
            )
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=settlement_limit,
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                time_range=None,
            )
            return decision_page, settle_page

        decision_page, settle_page = await self._with_repositories(_query)
        return build_parameter_sweep(
            decisions=tuple(decision_page.items or ()),
            settlements=tuple(settle_page.items or ()),
            candidates=candidates,
            per_decision_usdc=per_decision_usdc,
        )

    async def missed_opportunities_snapshot(
        self,
        *,
        limit: int = 500,
        per_decision_usdc: Decimal = Decimal("10"),
        strategy_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """对 ``accepted=false`` 的决策做事后盈利模拟。

        join settlement 事件 + 决策 token_id 判断"如果当时下单了赚还是亏"，
        按 reason 聚合——配合 ``risk_rejections`` 用，判断风控阈值是否过严。
        """

        from polymarket_trader.app.missed_opportunities import build_missed_opportunities
        from polymarket_trader.domain.events import DomainEventType

        if not self._has_db_session_factory():
            return build_missed_opportunities(
                rejected_decisions=(),
                settlements=(),
                per_decision_usdc=per_decision_usdc,
            )

        async def _query(repos: _RepositoryGroup) -> tuple[Any, Any]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=limit,
                offset=0,
                accepted=False,
                strategy_id=strategy_id,
                time_range=time_range,
            )
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=max(limit, 500),
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                time_range=None,
            )
            return decision_page, settle_page

        decision_page, settle_page = await self._with_repositories(_query)
        return build_missed_opportunities(
            rejected_decisions=tuple(decision_page.items or ()),
            settlements=tuple(settle_page.items or ()),
            per_decision_usdc=per_decision_usdc,
        )

    async def get_market_settlement(
        self,
        *,
        condition_id: str,
    ) -> dict[str, Any] | None:
        """单市场最新 settlement——含 winning_token_id / outcome / 时间戳，加上
        我们最后一次的 fair_value 偏差（如果有对应的 accepted 决策）。

        给"市场资源化后我们的定价对不对"快速诊断。无结算返回 None → 404。
        """

        if not self._has_db_session_factory():
            return None

        from polymarket_trader.domain.events import DomainEventType

        async def _query(repos: _RepositoryGroup) -> tuple[Any, Any]:
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=1,
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                condition_id=condition_id,
            )
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=1,
                offset=0,
                condition_id=condition_id,
                accepted=True,
            )
            return settle_page, decision_page

        settle_page, decision_page = await self._with_repositories(_query)
        settle_items = tuple(settle_page.items or ())
        if not settle_items:
            return None
        settlement = settle_items[0]
        payload = settlement.payload if isinstance(settlement.payload, dict) else {}
        winning_token_id = payload.get("winning_token_id")
        last_decision = (
            tuple(decision_page.items or ())[0] if (decision_page.items or ()) else None
        )
        last_fair_value: str | None = None
        last_token_id: str | None = None
        deviation: str | None = None
        if last_decision is not None and isinstance(last_decision.decision_output, dict):
            fv = last_decision.decision_output.get("fair_value")
            last_token_id = last_decision.token_id
            if fv is not None:
                last_fair_value = str(fv)
                try:
                    actual = (
                        Decimal("1") if winning_token_id and last_token_id == winning_token_id else Decimal("0")
                    )
                    fair_dec = Decimal(str(fv))
                    deviation = str(actual - fair_dec)
                except Exception:
                    deviation = None
        return {
            "condition_id": condition_id,
            "settled_at": payload.get("settled_at"),
            "winning_token_id": winning_token_id,
            "winning_outcome": payload.get("winning_outcome"),
            "source": payload.get("source"),
            "operator": payload.get("operator"),
            "last_decision": {
                "record_id": None if last_decision is None else last_decision.record_id,
                "token_id": last_token_id,
                "fair_value": last_fair_value,
                "outcome_minus_fair": deviation,
            },
        }

    async def calibration_snapshot(
        self,
        *,
        bucket_size: Decimal = Decimal("0.05"),
        strategy_id: str | None = None,
        time_range: TimeRange | None = None,
        sample_limit: int = 2000,
    ) -> dict[str, Any]:
        """定价模型校准 + Brier score。

        对每个 ``accepted=true`` 的决策，按 ``decision_output.fair_value`` 分桶；
        每个桶在市场结算后统计实际命中率（市场 RESOLVED 且持仓 redeemable=true
        视为预测正确）。Brier 是 ``mean((fair_value - outcome)^2)``，越接近 0
        校准越好。
        """

        if not self._has_db_session_factory():
            return {
                "bucket_size": str(bucket_size),
                "buckets": [],
                "brier_score": None,
                "log_loss": None,
                "total_samples": 0,
                "with_outcome_count": 0,
            }

        from polymarket_trader.domain.events import DomainEventType

        async def _query(repos: _RepositoryGroup) -> tuple[Any, Any]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=sample_limit,
                offset=0,
                accepted=True,
                strategy_id=strategy_id,
                time_range=time_range,
            )
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=sample_limit,
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                time_range=None,
            )
            return decision_page, settle_page

        decision_page, settle_page = await self._with_repositories(_query)
        from polymarket_trader.app.calibration import build_calibration

        return build_calibration(
            decisions=tuple(decision_page.items or ()),
            settlements=tuple(settle_page.items or ()),
            bucket_size=bucket_size,
        )

    async def list_reconcile_diffs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
        include_started: bool = False,
        include_applied: bool = True,
    ) -> dict[str, Any]:
        """从 outbox_events 拉 reconcile 相关事件作为结构化 diff 视图。

        默认返回 ``reconcile_diff_detected``（每条差异 + action_type / target /
        pause_reason / metadata）和 ``reconcile_applied``（每次执行结果汇总）；
        要看每轮调度起点可加 ``include_started=true``。
        """

        from polymarket_trader.domain.events import DomainEventType

        event_types: list[str] = [DomainEventType.RECONCILE_DIFF_DETECTED.value]
        if include_applied:
            event_types.append(DomainEventType.RECONCILE_APPLIED.value)
        if include_started:
            event_types.append(DomainEventType.RECONCILE_STARTED.value)

        if not self._has_db_session_factory():
            page: RepositoryPage[Any] = RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().outbox_event)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.outbox.list_events_by_types_snapshot(
                event_types=tuple(event_types),
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                condition_id=condition_id,
                time_range=time_range,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=self._serializer().outbox_event)

    async def latency_percentiles_snapshot(
        self,
        *,
        window_ms: int | None = None,
        sample_limit: int = 500,
        event_types: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """计算订单执行 latency 分位数（queue→sign / sign→submit / submit→ack / queue→ack）。

        从 outbox_events.payload->'timestamps' 抽 ``queued_at`` /
        ``sign_started_at`` / ``signed_at`` / ``submitted_at`` / ``ack_at``，
        按事件 fan-out 计算 stage 间 latency 毫秒；P0 实时性观测刚需。
        """

        from polymarket_trader.domain.events import DomainEventType

        types = event_types or (
            DomainEventType.ORDER_SUBMITTED.value,
            DomainEventType.ORDER_SIGNED.value,
            DomainEventType.ORDER_MATCHED.value,
            DomainEventType.ORDER_PARTIALLY_FILLED.value,
            DomainEventType.ORDER_NO_FILL.value,
            DomainEventType.ORDER_REJECTED.value,
            DomainEventType.ORDER_CANCEL_REQUESTED.value,
            DomainEventType.ORDER_CANCELLED.value,
            DomainEventType.REPLACE_ORDER_SUBMITTED.value,
        )

        time_range: TimeRange | None = None
        if window_ms is not None and window_ms > 0:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            time_range = TimeRange(since_ms=now_ms - window_ms, until_ms=now_ms)

        if not self._has_db_session_factory():
            return _empty_latency_payload(types, sample_limit, window_ms)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.outbox.list_events_by_types_snapshot(
                event_types=types,
                limit=sample_limit,
                offset=0,
                time_range=time_range,
            )

        page = await self._with_repositories(_query)
        events = tuple(page.items or ())
        return _compute_latency_payload(events, types, sample_limit, window_ms)

    async def portfolio_risk_metrics(
        self,
        *,
        window_ms: int,
        interval_ms: int,
        account_key: str = "primary",
        annualization_factor: float | None = None,
    ) -> dict[str, Any]:
        """组合级风险归因——max drawdown / time underwater / 波动率 / Sharpe-like。

        复用 ``equity-curve`` 同一份 downsampled 时间序列；空 / 单点序列也
        安全（相关指标返回 None 而不是抛错）。``annualization_factor`` 可
        选，如 365 表示按 1d 桶年化 Sharpe。
        """

        if not self._has_db_session_factory():
            raise RuntimeError("db_session_factory unavailable")

        session_factory = self.runtime.db_session_factory  # type: ignore[union-attr]

        from polymarket_trader.app.portfolio_history_service import (
            PortfolioHistoryService,
        )
        from polymarket_trader.app.risk_metrics import build_risk_metrics
        from polymarket_trader.infra.db import AccountSnapshotRepository

        async def _query(*, since, until, interval_ms, account_key):
            async with session_factory() as session:
                repo = AccountSnapshotRepository(session)
                return await repo.query_history_bucketed(
                    since=since,
                    until=until,
                    interval_ms=interval_ms,
                    account_key=account_key,
                )

        service = PortfolioHistoryService(query_history=_query)
        result = await service.equity_curve(
            window_ms=window_ms,
            interval_ms=interval_ms,
            account_key=account_key,
        )
        return {
            "window_ms": result.window_ms,
            "interval_ms": result.interval_ms,
            "metrics": build_risk_metrics(
                result.points,
                annualization_factor=annualization_factor,
            ),
        }

    async def portfolio_equity_curve(
        self,
        *,
        window_ms: int,
        interval_ms: int,
        account_key: str = "primary",
    ) -> dict[str, Any]:
        """返回 ``GET /portfolio/equity-curve`` 投影。

        Downsampling 在 PG 内完成；服务层只对已聚合点做 drawdown 投影。
        没有 DB 时抛 ``RuntimeError``——这条接口本身就是审计用途，缺少持久化
        数据的语义不能用空数组掩盖。
        """

        if not self._has_db_session_factory():
            raise RuntimeError("db_session_factory unavailable")

        session_factory = self.runtime.db_session_factory  # type: ignore[union-attr]

        from polymarket_trader.app.portfolio_history_service import (
            PortfolioHistoryService,
        )
        from polymarket_trader.infra.db import AccountSnapshotRepository

        async def _query(*, since, until, interval_ms, account_key):
            async with session_factory() as session:
                repo = AccountSnapshotRepository(session)
                return await repo.query_history_bucketed(
                    since=since,
                    until=until,
                    interval_ms=interval_ms,
                    account_key=account_key,
                )

        service = PortfolioHistoryService(query_history=_query)
        result = await service.equity_curve(
            window_ms=window_ms,
            interval_ms=interval_ms,
            account_key=account_key,
        )
        return result.to_payload()

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

    async def get_decision_record(self, record_id: str) -> dict[str, Any] | None:
        """按 ``record_id`` 取单条策略决策详情。

        相对 ``/admin/decisions/dump`` 的列表分页，这里返回单条 + 完整
        ``decision_input`` / ``decision_output`` JSONB——便于从 trade timeline
        点开后做"为什么决定/拒绝"的根因追查。
        """

        if not self._has_db_session_factory():
            return None

        async def _query(repos: _RepositoryGroup) -> DecisionRecord | None:
            return await repos.decision.get_by_record_id(record_id)

        record = await self._with_repositories(_query)
        return None if record is None else _decision_record_payload(record)

    async def list_decisions(
        self,
        *,
        limit: int = 1000,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        accepted: bool | None = None,
        time_range: TimeRange | None = None,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """暴露 ``decision_records`` 表（策略 hook 决策录制）。

        DB 是决策历史唯一真相来源——内存 ring buffer 已删除，无 DB 时返回空集，
        不再退化到本地缓存，避免实盘场景"看似还能 dump 但少了几小时事件"的歧义。
        """

        if not self._has_db_session_factory():
            page: RepositoryPage[Any] = RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
            return page_payload(page, serializer=_decision_record_payload)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.decision.list_decisions_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                condition_id=condition_id,
                accepted=accepted,
                time_range=time_range,
                strategy_id=strategy_id,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=_decision_record_payload)


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
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """从热态 market、orderbook 与直播 metadata 投影体育扫尾候选。"""

        candidates: list[dict[str, Any]] = []
        account = self._account_snapshot()
        # candidates 在运行时纯内存投影，归属由 runtime.extension.spec.strategy_id 决定。
        # 入参 strategy_id 与运行时不一致时直接返回空集——避免不同策略 id 之间漂移。
        runtime_strategy_id = self._runtime_strategy_id()
        if strategy_id is not None and runtime_strategy_id is not None and strategy_id != runtime_strategy_id:
            empty_page = self._slice_sequence((), limit=limit, offset=offset)
            payload = page_payload(empty_page, serializer=lambda item: item)
            payload["has_more"] = False
            payload["source_markets"] = 0
            return payload
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


