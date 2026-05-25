"""Runtime / workers / health / metrics / portfolio 维度的 admin 只读查询。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_trader.app.admin_serialization import decimal_text, jsonable
from polymarket_trader.app.admin_service_helpers import _RepositoryGroup
from polymarket_trader.infra.db import RepositoryPage


class AdminRuntimeQueryMixin:
    """Runtime / workers / metrics / health / portfolio 等只读快照。"""

    def health_snapshot(self) -> dict[str, Any]:
        return self._runtime_view().health_snapshot()

    def readiness_snapshot(self) -> dict[str, Any]:
        return self._runtime_view().readiness_snapshot()

    async def runtime_snapshot(self) -> dict[str, Any]:
        return await self._runtime_view().runtime_snapshot()

    def workers_snapshot(self) -> dict[str, Any]:
        return self._runtime_view().workers_snapshot()

    def metrics_snapshot(self) -> dict[str, Any]:
        return self._runtime_view().metrics_snapshot()

    async def portfolio_snapshot(self) -> dict[str, Any]:
        account = self._account_snapshot()
        base = self._portfolio_pnl_aggregates(account)

        if not self._has_db_session_factory():
            return {**base, "recent_allocations": []}

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.allocation.list_allocations_snapshot(limit=50, offset=0)

        allocations = await self._with_repositories(_query)
        return {
            **base,
            "recent_allocations": [
                self._serializer().allocation(allocation) for allocation in allocations.items
            ],
        }

    def _portfolio_pnl_aggregates(self, account: Any) -> dict[str, Any]:
        """从内存仓位聚合 PnL；零 DB、零 P0 影响。"""
        positions = account.positions
        zero = Decimal("0")
        total_cost = sum((p.cost_usdc for p in positions), zero)
        total_current_value = sum((p.current_value or zero for p in positions), zero)
        total_cash_pnl = sum((p.cash_pnl or zero for p in positions), zero)
        total_realized_pnl = sum((p.realized_pnl or zero for p in positions), zero)
        open_position_count = sum(1 for p in positions if p.shares > zero)
        net_value_usdc = account.balance_usdc + total_current_value
        registry = self._registry_snapshot()
        return {
            "balance_usdc": decimal_text(account.balance_usdc),
            "allowance_usdc": decimal_text(account.allowance_usdc),
            "available_usdc": decimal_text(account.available_usdc),
            "net_value_usdc": decimal_text(net_value_usdc),
            "notional_usdc": decimal_text(total_current_value),
            "cost_usdc": decimal_text(total_cost),
            "cash_pnl_usdc": decimal_text(total_cash_pnl),
            "realized_pnl_usdc": decimal_text(total_realized_pnl),
            "position_count": len(positions),
            "open_position_count": open_position_count,
            "open_order_count": len(account.open_orders),
            "fill_count": len(account.fills),
            "pause_count": len(account.market_pauses),
            "last_reconcile_at": jsonable(account.last_reconcile_at),
            "user_ws_connected": account.user_ws_connected,
            "allow_new_entries": account.allow_new_entries,
            "markets_tracked": len(registry.markets),
        }


    def outbox_queue_depth(self) -> dict[str, Any]:
        """事件队列积压深度（纯内存，零 DB，零 P0 影响）。

        从 runtime.event_bus 读取三条 lane（trading / maintenance / persistence）
        的实时队列深度与容量。深度/容量比过高表明持久化链路出现瓶颈。
        """

        event_bus = self.runtime.event_bus if self.runtime else None
        if event_bus is None:
            return {"available": False}

        snap = event_bus.snapshot()
        def _pct(depth: int, cap: int) -> str | None:
            if cap <= 0:
                return None
            return decimal_text(Decimal(str(round(depth / cap * 100, 1))))

        return {
            "available": True,
            "trading": {
                "depth": snap.trading_queue_depth,
                "capacity": snap.trading_queue_capacity,
                "retained": snap.trading_retained_depth,
                "utilization_pct": _pct(snap.trading_queue_depth, snap.trading_queue_capacity),
            },
            "maintenance": {
                "depth": snap.maintenance_queue_depth,
                "capacity": snap.maintenance_queue_capacity,
                "retained": snap.maintenance_retained_depth,
                "utilization_pct": _pct(snap.maintenance_queue_depth, snap.maintenance_queue_capacity),
            },
            "persistence": {
                "depth": snap.persistence_queue_depth,
                "capacity": snap.persistence_queue_capacity,
                "retained": snap.persistence_retained_depth,
                "utilization_pct": _pct(snap.persistence_queue_depth, snap.persistence_queue_capacity),
            },
            "low_priority_paused": snap.low_priority_paused,
        }

    def data_freshness(self) -> dict[str, Any]:
        """每市场数据源新鲜度快照（纯内存，零 DB，零 P0 影响）。

        从 market_metadata_store 读取每条 metadata record 的最后更新时间，
        计算 staleness_ms。staleness 过高说明 live_state 数据源断流。
        """

        store = self._entry_metadata_store()
        if store is None:
            return {"available": False, "items": []}

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        items = []

        for record in store.records():
            updated_at = record.updated_at
            age_ms = None
            try:
                age_ms = now_ms - int(updated_at.timestamp() * 1000)
            except Exception:
                pass

            items.append({
                "condition_id": record.condition_id,
                "market_slug": record.market_slug,
                "event_slug": record.event_slug,
                "source": record.source,
                "has_live_state": bool(record.live_state_payload),
                "signal_allowed": record.live_state_signal_allowed,
                "staleness_ms": age_ms,
                "updated_at": jsonable(updated_at),
            })

        # 按 staleness 倒序——最旧的最需要关注
        items.sort(key=lambda x: x["staleness_ms"] if x["staleness_ms"] is not None else -1, reverse=True)
        return {
            "available": True,
            "item_count": len(items),
            "items": items,
        }


__all__ = ["AdminRuntimeQueryMixin"]
