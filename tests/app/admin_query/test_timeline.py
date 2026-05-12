"""AdminTimelineQueryMixin 直接 unit 测试。

覆盖：
- ``list_audit_events`` 无 DB 时返回空 page
- ``list_audit_events`` 经 _with_repositories 拉到事件后用 serializer 投影
- ``list_trade_replays`` 无 DB 时从内存 snapshot 构造 records
- ``get_trade_timeline`` 无 DB 时返回空 envelope
- ``aggregate_operator_interventions`` 无 DB 时返回空聚合
- ``aggregate_operator_interventions`` 聚合 operator 维度统计
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from polymarket_trader.app.admin_query.timeline import AdminTimelineQueryMixin
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.runtime.registry import MarketRegistrySnapshot


@dataclass
class _StubAuditEvent:
    event_title: str
    payload: dict[str, Any]
    event_id: str = "ev"
    trace_id: str = "tr"
    reason: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime(2026, 5, 11, tzinfo=timezone.utc))


@dataclass
class _FakeSerializer:
    def audit_event(self, event: Any) -> dict[str, Any]:
        return {"event_id": event.event_id, "event_title": event.event_title}

    # build_trade_replay_records 需要——签名上接近 AdminSerializer 子集
    def order(self, order: Any) -> dict[str, Any]:
        return {"order_id": order.order_id}

    def fill(self, fill: Any) -> dict[str, Any]:
        return {"trade_id": fill.trade_id}

    def position(self, position: Any) -> dict[str, Any]:
        return {"token_id": position.token_id}

    def market(self, market: Any) -> dict[str, Any]:
        return {"condition_id": market.condition_id}


@dataclass
class _Host(AdminTimelineQueryMixin):
    has_db: bool = False
    audit_page: RepositoryPage[Any] | None = None
    account: AccountSnapshot = field(default_factory=AccountSnapshot)
    registry: MarketRegistrySnapshot = field(default_factory=lambda: MarketRegistrySnapshot(markets=()))

    def __post_init__(self) -> None:
        self._ser = _FakeSerializer()

    def _has_db_session_factory(self) -> bool:
        return self.has_db

    def _serializer(self) -> _FakeSerializer:
        return self._ser

    def _account_snapshot(self) -> AccountSnapshot:
        return self.account

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        return self.registry

    def _slice_sequence(self, items: Any, *, limit: int, offset: int) -> RepositoryPage[Any]:
        items_list = tuple(items)
        sliced = items_list[offset : offset + limit]
        return RepositoryPage(items=sliced, total=len(items_list), limit=limit, offset=offset)

    async def _with_repositories(self, callback: Callable[[Any], Any]) -> Any:
        if self.audit_page is not None:
            return self.audit_page
        raise AssertionError("DB path triggered without audit_page configured")


def test_list_audit_events_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.list_audit_events(limit=10, offset=0))
    assert payload == {"items": [], "total": 0, "limit": 10, "offset": 0}


def test_list_audit_events_serializes_page_when_db_present() -> None:
    events = (
        _StubAuditEvent(event_title="risk_rejection_recorded", payload={}, event_id="e1"),
        _StubAuditEvent(event_title="order_submitted", payload={}, event_id="e2"),
    )
    host = _Host(
        has_db=True,
        audit_page=RepositoryPage(items=events, total=2, limit=50, offset=0),
    )
    payload = asyncio.run(host.list_audit_events(limit=50, offset=0))
    assert payload["total"] == 2
    assert payload["items"][0]["event_id"] == "e1"
    assert payload["items"][1]["event_title"] == "order_submitted"


def test_list_trade_replays_empty_when_no_db_and_empty_account() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.list_trade_replays(limit=10, offset=0))
    assert payload == {"items": [], "total": 0, "limit": 10, "offset": 0}


def test_get_trade_timeline_returns_empty_envelope_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.get_trade_timeline(condition_id="cond-A"))
    assert payload == {
        "condition_id": "cond-A",
        "token_id": None,
        "event_count": 0,
        "truncated": False,
        "events": [],
        "current_position": None,
    }


def test_aggregate_operator_interventions_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.aggregate_operator_interventions())
    assert payload == {
        "operator": None,
        "total_events": 0,
        "by_operator": [],
        "by_event_title": [],
        "events": [],
    }


def test_aggregate_operator_interventions_counts_by_operator_and_title() -> None:
    events = (
        _StubAuditEvent(
            event_title="trading_paused",
            payload={"operator": "alice"},
            event_id="e1",
        ),
        _StubAuditEvent(
            event_title="trading_paused",
            payload={"operator": "alice"},
            event_id="e2",
        ),
        _StubAuditEvent(
            event_title="market_paused",
            payload={"operator": "bob"},
            event_id="e3",
        ),
        _StubAuditEvent(
            event_title="other",
            payload={},  # 无 operator —— 必须跳过
            event_id="e4",
        ),
    )
    host = _Host(
        has_db=True,
        audit_page=RepositoryPage(items=events, total=4, limit=2000, offset=0),
    )
    payload = asyncio.run(host.aggregate_operator_interventions())
    assert payload["total_events"] == 3  # e4 没有 operator
    by_op = {row["operator"]: row["count"] for row in payload["by_operator"]}
    assert by_op == {"alice": 2, "bob": 1}
    by_title = {row["event_title"]: row["count"] for row in payload["by_event_title"]}
    assert by_title == {"trading_paused": 2, "market_paused": 1}


def test_aggregate_operator_interventions_filters_by_operator_argument() -> None:
    events = (
        _StubAuditEvent(event_title="x", payload={"operator": "alice"}, event_id="e1"),
        _StubAuditEvent(event_title="y", payload={"operator": "bob"}, event_id="e2"),
    )
    host = _Host(
        has_db=True,
        audit_page=RepositoryPage(items=events, total=2, limit=2000, offset=0),
    )
    payload = asyncio.run(host.aggregate_operator_interventions(operator="bob"))
    assert payload["total_events"] == 1
    assert payload["by_operator"] == [{"operator": "bob", "count": 1}]
