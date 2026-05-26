"""ReconcileDecisionsAggregator —— reconcile diff 视图 + 决策录制查询。

按 原架构方案 §12.2 审计查询类（走 DB）。decision_records 是策略
hook 决策的唯一真相（内存 ring buffer 已删），本 aggregator 只暴露 GET，
落库在 decision_recorder worker。

# Endpoint 对应

| Endpoint | 方法 | 内容 |
|---|---|---|
| `GET /runtime/reconcile/diffs` | `list_reconcile_diffs(...)` | outbox 中 reconcile 事件结构化视图 |
| `GET /runtime/decisions/dump` | `list_decisions(...)` | decision_records 分页 |
| `GET /runtime/decisions/{rid}` | `get_decision_record(rid)` | 单条决策完整 input/output |
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from polymarket_trader.serialization import page_payload
from polymarket_trader.api.serialization import ApiSerializer
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage

from ._db import RepositoryGroup, with_repositories
from ._helpers import decision_record_payload as _decision_record_payload

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ReconcileDecisionsAggregator:
    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession] | None",
        serializer: ApiSerializer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._serializer = serializer or ApiSerializer.from_runtime(None)

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
        """从 outbox_events 拉 reconcile 相关事件作为结构化 diff 视图。"""

        event_types: list[str] = [DomainEventType.RECONCILE_DIFF_DETECTED.value]
        if include_applied:
            event_types.append(DomainEventType.RECONCILE_APPLIED.value)
        if include_started:
            event_types.append(DomainEventType.RECONCILE_STARTED.value)

        if self._session_factory is None:
            empty: RepositoryPage[Any] = RepositoryPage(
                items=tuple(), total=0, limit=limit, offset=offset
            )
            return page_payload(empty, serializer=self._serializer.outbox_event)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.outbox.list_events_by_types_snapshot(
                event_types=tuple(event_types),
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                condition_id=condition_id,
                time_range=time_range,
            )

        page = await with_repositories(self._session_factory, _query)
        return page_payload(page, serializer=self._serializer.outbox_event)

    async def list_decisions(
        self,
        *,
        limit: int = 1000,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        accepted: bool | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """暴露 decision_records 表（策略 hook 决策录制）。"""

        if self._session_factory is None:
            empty: RepositoryPage[Any] = RepositoryPage(
                items=tuple(), total=0, limit=limit, offset=offset
            )
            return page_payload(empty, serializer=_decision_record_payload)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.decision.list_decisions_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                condition_id=condition_id,
                accepted=accepted,
                time_range=time_range,
            )

        page = await with_repositories(self._session_factory, _query)
        return page_payload(page, serializer=_decision_record_payload)

    async def get_decision_record(self, record_id: str) -> dict[str, Any] | None:
        """按 record_id 取单条策略决策详情。"""

        if self._session_factory is None:
            return None

        async def _query(repos: RepositoryGroup) -> DecisionRecord | None:
            return await repos.decision.get_by_record_id(record_id)

        record = await with_repositories(self._session_factory, _query)
        return None if record is None else _decision_record_payload(record)
