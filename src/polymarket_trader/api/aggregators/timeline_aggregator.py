"""TimelineAggregator —— audit 事件 / 复盘 / 时间线查询（§12.2 审计查询类，不缓存）。

# Endpoint 对应

| Endpoint | 方法 | 内容 |
|---|---|---|
| `GET /audit-events` | `list_audit_events(...)` | 按 trace_id/event_title/condition_id 等过滤 |
| `GET /audit-events/operators` | `aggregate_operator_interventions(...)` | 人工干预分布 |
| `GET /trade-replays` | `list_trade_replays(...)` | 成交 + 持仓 + 审计聚合复盘 |
| `GET /trades/{cid}/timeline` | `get_trade_timeline(...)` | 单市场完整时间线 |

# 设计

aggregator 持 `session_factory` + `runtime`（用于无 DB 时降级到内存快照）+
`serializer`（共享 admin_serialization 复用，不为了 §10 强行拆——serializer
是稳定 utility，多 aggregator 共享反而消除重复）。

`session_factory=None` 时 → 返回空 page（前端友好降级，不抛 500）。
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from polymarket_trader.serialization import jsonable, page_payload
from polymarket_trader.app.admin_serialization import AdminSerializer
from polymarket_trader.domain.analytics.trade_replay import TradeReplayFilters, build_trade_replay_records
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage

from ._db import RepositoryGroup, with_repositories
from ._helpers import slice_sequence

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class TimelineAggregator:
    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession] | None",
        runtime: Any = None,
        serializer: AdminSerializer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runtime = runtime
        self._serializer = serializer or AdminSerializer.from_runtime(runtime)

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
        if self._session_factory is None:
            empty_page: RepositoryPage[Any] = RepositoryPage(
                items=tuple(), total=0, limit=limit, offset=offset
            )
            return page_payload(empty_page, serializer=self._serializer.audit_event)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
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

        page = await with_repositories(self._session_factory, _query)
        return page_payload(page, serializer=self._serializer.audit_event)

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
        if self._session_factory is None:
            # 无 DB 降级到内存快照
            account = (
                self._runtime.account_state_store.snapshot()
                if (self._runtime is not None and getattr(self._runtime, "account_state_store", None) is not None)
                else AccountSnapshot()
            )
            registry_snapshot = (
                self._runtime.registry.snapshot()
                if (self._runtime is not None and getattr(self._runtime, "registry", None) is not None)
                else MarketRegistrySnapshot(tuple())
            )
            records = build_trade_replay_records(
                markets=registry_snapshot.markets,
                orders=account.open_orders,
                fills=account.fills,
                positions=account.positions,
                audit_events=(),
                serializer=self._serializer,
                filters=filters,
            )
            page = slice_sequence(records, limit=limit, offset=offset)
            return page_payload(page, serializer=lambda item: item)

        async def _query(repos: RepositoryGroup) -> dict[str, Any]:
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
                serializer=self._serializer,
                filters=filters,
            )
            page = slice_sequence(records, limit=limit, offset=offset)
            return page_payload(page, serializer=lambda item: item)

        return await with_repositories(self._session_factory, _query)

    async def get_trade_timeline(
        self,
        *,
        condition_id: str,
        token_id: str | None = None,
        time_range: TimeRange | None = None,
        limit: int = 1000,
        per_table_limit: int = 1000,
    ) -> dict[str, Any]:
        """单笔交易/单个市场全生命周期 timeline。"""

        from polymarket_trader.domain.analytics.trade_timeline import (
            TradeTimelineInputs,
            build_trade_timeline,
        )
        from polymarket_trader.domain.events import DomainEventType

        if self._session_factory is None:
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

        async def _query(repos: RepositoryGroup) -> TradeTimelineInputs:
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

        inputs = await with_repositories(self._session_factory, _query)
        return build_trade_timeline(
            condition_id=condition_id,
            token_id=token_id,
            inputs=inputs,
            serializer=self._serializer,
            limit=limit,
        )

    async def aggregate_operator_interventions(
        self,
        *,
        operator: str | None = None,
        time_range: TimeRange | None = None,
        sample_limit: int = 2000,
    ) -> dict[str, Any]:
        """按 operator 聚合人工干预——audit_events.payload 里有 operator 字段。"""

        if self._session_factory is None:
            return {
                "operator": operator,
                "total_events": 0,
                "by_operator": [],
                "by_event_title": [],
                "events": [],
            }

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.audit.list_audit_events_snapshot(
                limit=sample_limit,
                offset=0,
                time_range=time_range,
            )

        page = await with_repositories(self._session_factory, _query)
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
