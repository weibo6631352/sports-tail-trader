"""交易维度的 admin 只读查询：订单、成交、持仓、分配、风控拒绝、组合暴露。"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from polymarket_trader.app.admin_serialization import decimal_text, page_payload
from polymarket_trader.app.admin_service_helpers import _RepositoryGroup
from polymarket_trader.domain.account import (
    AccountSnapshot,
    MarketPauseSource,
    _open_buy_order_reserved_usdc,
)
from polymarket_trader.domain.order import OrderSide
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
                and (status is None or (order.status is not None and order.status.value == status))
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
                status=status,
                time_range=time_range,
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
        # 内存快照是唯一真相来源（§3）；DB 仅审计/复盘，不作当前持仓 fallback。
        # 启动竞态窗口（首次 reconcile 前）返回空而不是旧快照，避免 React Query 缓存旧数据后
        # 每次 reconcile_applied SSE 触发 invalidate 时产生 24→0 闪烁。
        snapshot = self._account_snapshot()
        positions = [
            position
            for position in snapshot.positions
            if (condition_id is None or position.condition_id == condition_id)
            and (token_id is None or position.token_id == token_id)
        ]
        page = self._slice_sequence(positions, limit=limit, offset=offset)
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


    async def portfolio_exposure(self) -> dict[str, Any]:
        """每市场未平仓名义暴露快照（纯内存，零 DB，零 P0 影响）。

        按 condition_id 分组，返回每个仓位的：名义市值、浮动盈亏、
        平均入场价、当前价、挂单预留资金、暂停状态。
        操盘者最直接的"我的风险在哪里"全局视图。
        """

        snapshot = self._account_snapshot()

        # 预聚合每个 (condition_id, token_id) 的挂单预留 USDC
        reserved_by_token: dict[tuple[str, str], Decimal] = {}
        for order in snapshot.open_orders:
            if order.side != OrderSide.BUY or not order.open:
                continue
            key = (order.condition_id or "", order.token_id or "")
            reserved_by_token[key] = (
                reserved_by_token.get(key, Decimal("0"))
                + _open_buy_order_reserved_usdc(order)
            )

        total_notional = Decimal("0")
        total_cost = Decimal("0")
        total_cash_pnl = Decimal("0")
        total_reserved = Decimal("0")
        items = []

        for pos in snapshot.positions:
            notional = (
                pos.current_value
                if pos.current_value is not None
                else pos.cost_usdc
            )
            reserved = reserved_by_token.get(
                (pos.condition_id, pos.token_id), Decimal("0")
            )
            total_notional += notional
            total_cost += pos.cost_usdc
            total_reserved += reserved
            if pos.cash_pnl is not None:
                total_cash_pnl += pos.cash_pnl

            items.append({
                "condition_id": pos.condition_id,
                "token_id": pos.token_id,
                "market_slug": pos.market_slug,
                "shares": decimal_text(pos.shares),
                "cost_usdc": decimal_text(pos.cost_usdc),
                "notional_usdc": decimal_text(notional),
                "avg_price": decimal_text(pos.avg_price) if pos.avg_price is not None else None,
                "cur_price": decimal_text(pos.cur_price) if pos.cur_price is not None else None,
                "cash_pnl": decimal_text(pos.cash_pnl) if pos.cash_pnl is not None else None,
                "percent_pnl": decimal_text(pos.percent_pnl) if pos.percent_pnl is not None else None,
                "realized_pnl": decimal_text(pos.realized_pnl) if pos.realized_pnl is not None else None,
                "open_buy_reserved_usdc": decimal_text(reserved),
                # paused 字段只反映"人工点击触发"的暂停（MarketPauseSource.MANUAL）。
                # 后台 reconcile / risk / strategy 自动 pause 是内部交易控制状态，
                # 与"我手上的仓位"无关，不在这里暴露——查这些状态走 /markets。
                "paused": _is_manually_paused(snapshot, pos.condition_id),
                "redeemable": pos.redeemable,
                "settled_zero_value": pos.settled_zero_value,
            })

        return {
            "items": items,
            "position_count": len(items),
            "total_notional_usdc": decimal_text(total_notional),
            "total_cost_usdc": decimal_text(total_cost),
            "total_cash_pnl": decimal_text(total_cash_pnl),
            "total_open_buy_reserved_usdc": decimal_text(total_reserved),
            "available_usdc": decimal_text(snapshot.available_usdc),
            "balance_usdc": decimal_text(snapshot.balance_usdc),
            "equity_usdc": decimal_text(snapshot.equity_usdc),
        }


def _is_manually_paused(snapshot: AccountSnapshot, condition_id: str) -> bool:
    """仓位 payload 的 paused 字段语义：仅在人工 click pause 时为 True。

    后台自动 pause（reconcile/risk/strategy 触发的 auto_quarantine_dead_market /
    market_not_tradable / unexpected_resting_order 等）属于内部交易控制状态，
    与"我手上的仓位"无关，不在仓位行展示——避免把"市场状态"挤进"仓位状态"。
    """
    pause = snapshot.pause_for_market(condition_id)
    return pause is not None and pause.source == MarketPauseSource.MANUAL


__all__ = ["AdminTradingQueryMixin"]
