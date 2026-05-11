"""结算 / 对账差异 / outbox / 决策录制 / 策略候选 维度的 admin 只读查询。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from polymarket_trader.app.admin_query._helpers import _decision_record_payload
from polymarket_trader.app.admin_serialization import page_payload
from polymarket_trader.app.admin_service_helpers import (
    _RepositoryGroup,
    _candidate_matches_filters,
)
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage


class AdminReconcileDecisionsQueryMixin:
    """结算 / 对账 / outbox / 决策录制 / 策略候选 维度的 admin 只读查询。"""

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


__all__ = ["AdminReconcileDecisionsQueryMixin"]
