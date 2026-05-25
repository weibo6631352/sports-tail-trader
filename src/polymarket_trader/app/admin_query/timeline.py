"""审计事件 / 复盘 / 时间线 维度的 admin 只读查询。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from polymarket_trader.app.admin_serialization import jsonable, page_payload
from polymarket_trader.app.admin_service_helpers import _RepositoryGroup
from polymarket_trader.app.trade_replay import TradeReplayFilters, build_trade_replay_records
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage


class AdminTimelineQueryMixin:
    """审计事件、复盘视图、单市场时间线、人工干预聚合。"""

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
        with_total: bool = False,
    ) -> dict[str, Any]:
        import time as _time
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
                with_total=with_total,
            )

        t0 = _time.perf_counter()
        page = await self._with_repositories(_query)
        db_ms = (_time.perf_counter() - t0) * 1000
        t1 = _time.perf_counter()
        payload = page_payload(page, serializer=self._serializer().audit_event)
        ser_ms = (_time.perf_counter() - t1) * 1000
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            mon = SystemPerfMonitor.get()
            mon.record_endpoint_step("audit_events", "db_with_repos", db_ms)
            mon.record_endpoint_step("audit_events", "serialize", ser_ms)
            mon.record_endpoint_step("audit_events", f"limit_{limit}", db_ms + ser_ms)
        except Exception:
            pass
        return payload

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


__all__ = ["AdminTimelineQueryMixin"]
