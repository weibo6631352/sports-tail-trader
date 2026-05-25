"""AdminTradingQueryMixin 直接 unit 测试。

覆盖：
- ``list_orders`` open_only=True 时只取热态；condition_id/token_id 过滤
- ``list_orders`` open_only=False 无 DB 回退到热态
- ``list_fills`` 无 DB 回退；strategy_id / trace_id 过滤
- ``list_positions`` snapshot.positions 非空时走热态、空 + 未对账 + 有 DB 时走 DB
- ``list_allocations`` 无 DB 时返回空
- ``aggregate_risk_rejections`` 聚合 check_counter
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from polymarket_trader.app.admin_query.trading import AdminTradingQueryMixin
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.order import OrderRecord, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.position import Position
from polymarket_trader.infra.db import RepositoryPage


def _order(token_id: str, condition_id: str = "cond-A", trace_id: str = "tr-1") -> OrderRecord:
    return OrderRecord(
        strategy_id="sports_tail",
        condition_id=condition_id,
        token_id=token_id,
        side=OrderSide.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.5"),
        trace_id=trace_id,
        size_shares=Decimal("10"),
        remaining_shares=Decimal("10"),
        order_id=f"ord-{token_id}",
        status=OrderStatus.LIVE,
        created_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )


def _fill(token_id: str, trace_id: str = "tr-1") -> Fill:
    return Fill(
        strategy_id="sports_tail",
        trace_id=trace_id,
        condition_id="cond-A",
        token_id=token_id,
        order_id=f"ord-{token_id}",
        trade_id=f"trd-{token_id}",
        side="BUY",
        price=Decimal("0.5"),
        size=Decimal("10"),
        notional_usdc=Decimal("5"),
    )


def _position(token_id: str) -> Position:
    return Position(
        strategy_id="sports_tail",
        condition_id="cond-A",
        token_id=token_id,
        shares=Decimal("10"),
        cost_usdc=Decimal("5"),
    )


@dataclass
class _FakeSerializer:
    def order(self, order: OrderRecord) -> dict[str, Any]:
        return {"order_id": order.order_id, "trace_id": order.trace_id}

    def fill(self, fill: Fill) -> dict[str, Any]:
        return {"order_id": fill.order_id, "trade_id": fill.trade_id}

    def position(self, position: Position) -> dict[str, Any]:
        return {"token_id": position.token_id, "shares": str(position.shares)}

    def allocation(self, allocation: Any) -> dict[str, Any]:
        return {"marker": getattr(allocation, "marker", None)}


@dataclass
class _Host(AdminTradingQueryMixin):
    snapshot: AccountSnapshot = field(default_factory=AccountSnapshot)
    has_db: bool = False
    db_page: RepositoryPage[Any] | None = None

    def __post_init__(self) -> None:
        self._ser = _FakeSerializer()

    def _account_snapshot(self) -> AccountSnapshot:
        return self.snapshot

    def _has_db_session_factory(self) -> bool:
        return self.has_db

    def _serializer(self) -> _FakeSerializer:
        return self._ser

    def _slice_sequence(self, items: Any, *, limit: int, offset: int) -> RepositoryPage[Any]:
        items_list = tuple(items)
        sliced = items_list[offset : offset + limit]
        return RepositoryPage(items=sliced, total=len(items_list), limit=limit, offset=offset)

    async def _with_repositories(self, callback: Callable[[Any], Any]) -> Any:
        if self.db_page is not None:
            return self.db_page
        raise AssertionError("DB path triggered without db_page configured")


def test_list_orders_open_only_filters_by_condition_id() -> None:
    snap = AccountSnapshot(open_orders=(_order("tk-A"), _order("tk-B", condition_id="cond-B")))
    host = _Host(snapshot=snap)
    payload = asyncio.run(host.list_orders(open_only=True, condition_id="cond-B"))
    assert payload["total"] == 1
    assert payload["items"][0]["order_id"] == "ord-tk-B"


def test_list_orders_open_only_paginates() -> None:
    orders = tuple(_order(f"tk-{i}") for i in range(4))
    snap = AccountSnapshot(open_orders=orders)
    host = _Host(snapshot=snap)
    payload = asyncio.run(host.list_orders(open_only=True, limit=2, offset=1))
    assert payload["total"] == 4
    assert len(payload["items"]) == 2
    assert payload["items"][0]["order_id"] == "ord-tk-1"


def test_list_orders_history_falls_back_to_memory_without_db() -> None:
    snap = AccountSnapshot(open_orders=(_order("tk-1"),))
    host = _Host(snapshot=snap, has_db=False)
    payload = asyncio.run(host.list_orders(open_only=False))
    assert payload["total"] == 1


def test_list_fills_memory_path_filters_by_trace_id() -> None:
    snap = AccountSnapshot(
        fills=(_fill("tk-1", trace_id="t1"), _fill("tk-2", trace_id="t2"))
    )
    host = _Host(snapshot=snap, has_db=False)
    payload = asyncio.run(host.list_fills(trace_id="t2"))
    assert payload["total"] == 1
    assert payload["items"][0]["trade_id"] == "trd-tk-2"


def test_list_positions_uses_memory_when_snapshot_not_empty() -> None:
    snap = AccountSnapshot(positions=(_position("tk-1"), _position("tk-2")))
    host = _Host(snapshot=snap, has_db=True)  # 即便 has_db=True，positions 非空走热态
    payload = asyncio.run(host.list_positions())
    assert payload["total"] == 2


def test_list_positions_uses_memory_after_reconcile_even_when_empty() -> None:
    # snapshot.positions 空 + last_reconcile_at 已对账：以热态空集为准，不再退化到 DB。
    snap = AccountSnapshot(
        positions=(),
        last_reconcile_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    host = _Host(snapshot=snap, has_db=True)
    payload = asyncio.run(host.list_positions())
    assert payload["total"] == 0


def test_portfolio_exposure_paused_flag_only_true_for_manual_pause() -> None:
    """仓位 payload 的 paused 字段只反映 MarketPauseSource.MANUAL；
    后台 reconcile/risk/strategy 自动 pause 不应让 paused=True。"""
    from polymarket_trader.domain.account import MarketPause, MarketPauseSource

    pos_manual = Position(
        strategy_id="sports_tail",
        condition_id="cond-MANUAL",
        token_id="tk-m",
        shares=Decimal("10"),
        cost_usdc=Decimal("5"),
    )
    pos_auto = Position(
        strategy_id="sports_tail",
        condition_id="cond-AUTO",
        token_id="tk-a",
        shares=Decimal("11"),
        cost_usdc=Decimal("6"),
    )
    pos_clean = Position(
        strategy_id="sports_tail",
        condition_id="cond-CLEAN",
        token_id="tk-c",
        shares=Decimal("12"),
        cost_usdc=Decimal("7"),
    )
    snap = AccountSnapshot(
        positions=(pos_manual, pos_auto, pos_clean),
        market_pauses=(
            MarketPause(
                condition_id="cond-MANUAL",
                reason="manual_pause",
                source=MarketPauseSource.MANUAL,
                recoverable=False,
            ),
            MarketPause(
                condition_id="cond-AUTO",
                reason="auto_quarantine_dead_market",
                source=MarketPauseSource.RECONCILE,
                recoverable=False,
            ),
        ),
    )
    host = _Host(snapshot=snap)
    payload = asyncio.run(host.portfolio_exposure())
    by_cid = {item["condition_id"]: item for item in payload["items"]}
    assert by_cid["cond-MANUAL"]["paused"] is True
    assert by_cid["cond-AUTO"]["paused"] is False  # 后台自动 pause 不暴露
    assert by_cid["cond-CLEAN"]["paused"] is False


def test_list_positions_never_falls_back_to_db_even_before_first_reconcile() -> None:
    # §3：内存快照是唯一真相来源；DB 只做审计/复盘，不作当前持仓 fallback。
    # 启动竞态窗口（首次 reconcile 前）返回空，避免读到旧的 DB 投影把 24→0 闪烁。
    snap = AccountSnapshot(positions=(), last_reconcile_at=None)
    db_page = RepositoryPage(items=(_position("tk-x"),), total=1, limit=100, offset=0)
    host = _Host(snapshot=snap, has_db=True, db_page=db_page)
    payload = asyncio.run(host.list_positions())
    assert payload["total"] == 0
    assert payload["items"] == []


def test_list_allocations_returns_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.list_allocations(limit=10, offset=0))
    assert payload == {"items": [], "total": 0, "limit": 10, "offset": 0}


def test_aggregate_risk_rejections_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.aggregate_risk_rejections())
    assert payload == {"buckets": [], "total_rejections": 0}


@dataclass
class _StubAuditEvent:
    """聚合逻辑只读 event.payload 与 event.event_title，stub 避免 AuditEvent
    构造路径把 dict 转 mappingproxy（聚合判 isinstance dict 即跳过）。"""

    event_title: str
    payload: dict[str, Any]
    event_id: str = "ev"
    trace_id: str = "tr"
    reason: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime(2026, 5, 11, tzinfo=timezone.utc))


def test_aggregate_risk_rejections_counts_failed_checks() -> None:
    events = (
        _StubAuditEvent(
            event_title="risk_rejection_recorded",
            payload={
                "checks": [
                    {"name": "balance", "field": "balance_usdc", "passed": False},
                    {"name": "balance", "field": "balance_usdc", "passed": False},
                    {"name": "max_orders", "field": "open_orders", "passed": True},
                ]
            },
        ),
        _StubAuditEvent(
            event_title="risk_rejection_recorded",
            payload={"checks": [{"name": "spread", "field": "spread_bps", "passed": False}]},
        ),
    )
    host = _Host(
        has_db=True,
        db_page=RepositoryPage(items=events, total=len(events), limit=1000, offset=0),
    )
    payload = asyncio.run(host.aggregate_risk_rejections())
    assert payload["total_rejections"] == 2
    by_check = {row["check_name"]: row["count"] for row in payload["by_check_name"]}
    assert by_check["balance"] == 2
    assert by_check["spread"] == 1
    # passed=True 的 check 不计数
    assert "max_orders" not in by_check
