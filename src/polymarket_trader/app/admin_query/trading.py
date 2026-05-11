"""交易维度的 admin 只读查询：订单、成交、持仓、分配、风控拒绝。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from polymarket_trader.app.admin_serialization import page_payload
from polymarket_trader.app.admin_service_helpers import _RepositoryGroup
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage


class AdminTradingQueryMixin:
    """订单 / 成交 / 持仓 / 资金分配 / 风控拒绝 维度的 admin 只读查询。"""

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


__all__ = ["AdminTradingQueryMixin"]
