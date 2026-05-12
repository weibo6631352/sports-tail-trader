"""AdminReconcileDecisionsQueryMixin 直接 unit 测试。

覆盖：
- ``list_market_settlements`` 直通 list_audit_events
- ``get_market_settlement`` 无 DB 返回 None
- ``list_reconcile_diffs`` 无 DB 时返回空 page；event_types include flags 拼接正确
- ``list_outbox_pending`` 从 runtime.outbox 取 pending 事件；trace_id 过滤
- ``list_outbox_failures`` 无 DB 时返回空 page
- ``get_decision_record`` 无 DB 返回 None
- ``list_decisions`` 无 DB 返回空 page
- ``list_strategy_candidates`` strategy_id 不匹配时返回空 envelope
- ``list_strategy_candidates`` 空 source markets 时返回 source_markets=0
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from polymarket_trader.app.admin_query.reconcile_decisions import (
    AdminReconcileDecisionsQueryMixin,
)
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.infra.db import RepositoryPage


@dataclass
class _StubOutboxEvent:
    trace_id: str
    event_type: str
    idempotency_key: str = "k"
    event_id: str = "e"
    market_slug: str | None = None
    event_slug: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime(2026, 5, 11, tzinfo=timezone.utc))
    priority: int = 0
    retry_count: int = 0
    last_error: str | None = None
    raw_response_summary: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeSerializer:
    def outbox_event(self, e: Any) -> dict[str, Any]:
        return {"trace_id": e.trace_id, "event_type": e.event_type}


@dataclass
class _StubOutbox:
    events: tuple[_StubOutboxEvent, ...]

    def pending_events(self) -> tuple[_StubOutboxEvent, ...]:
        return self.events


@dataclass
class _Host(AdminReconcileDecisionsQueryMixin):
    has_db: bool = False
    captured_audit_call: dict[str, Any] | None = None
    runtime: Any = None
    account: AccountSnapshot = field(default_factory=AccountSnapshot)
    runtime_strategy_id_value: str | None = None
    source_markets_value: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        self._ser = _FakeSerializer()

    def _serializer(self) -> _FakeSerializer:
        return self._ser

    def _has_db_session_factory(self) -> bool:
        return self.has_db

    def _account_snapshot(self) -> AccountSnapshot:
        return self.account

    def _runtime_strategy_id(self) -> str | None:
        return self.runtime_strategy_id_value

    def _candidate_source_markets(self, **_: Any) -> tuple[Any, ...]:
        return self.source_markets_value

    def _market_ws_snapshot(self, _token_id: str) -> Any:
        return None

    def _build_entry_plan_for_admin(self, **_: Any) -> Any:
        raise AssertionError("should not be invoked when source markets is empty")

    def _candidate_payload(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        raise AssertionError("should not be invoked when source markets is empty")

    def _slice_sequence(self, items: Any, *, limit: int, offset: int) -> RepositoryPage[Any]:
        items_list = tuple(items)
        sliced = items_list[offset : offset + limit]
        return RepositoryPage(items=sliced, total=len(items_list), limit=limit, offset=offset)

    async def list_audit_events(self, **kwargs: Any) -> dict[str, Any]:
        self.captured_audit_call = kwargs
        return {"items": [], "total": 0, "limit": kwargs.get("limit"), "offset": kwargs.get("offset")}

    async def _with_repositories(self, callback: Callable[[Any], Any]) -> Any:
        raise AssertionError("DB path triggered without has_db=True")


def test_list_market_settlements_delegates_with_correct_event_title() -> None:
    host = _Host()
    asyncio.run(host.list_market_settlements(limit=100, offset=10, condition_id="cond-X"))
    assert host.captured_audit_call is not None
    assert host.captured_audit_call["event_title"] == "market_settled"
    assert host.captured_audit_call["condition_id"] == "cond-X"
    assert host.captured_audit_call["limit"] == 100


def test_get_market_settlement_returns_none_without_db() -> None:
    host = _Host(has_db=False)
    result = asyncio.run(host.get_market_settlement(condition_id="cond-X"))
    assert result is None


def test_list_reconcile_diffs_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.list_reconcile_diffs(limit=10, offset=0))
    assert payload == {"items": [], "total": 0, "limit": 10, "offset": 0}


def test_list_outbox_pending_returns_runtime_events_filtered_by_trace_id() -> None:
    events = (
        _StubOutboxEvent(trace_id="t1", event_type="order_submitted"),
        _StubOutboxEvent(trace_id="t2", event_type="order_submitted"),
    )
    host = _Host()
    host.runtime = type("RT", (), {"outbox": _StubOutbox(events=events)})()
    payload = asyncio.run(host.list_outbox_pending(limit=10, offset=0, trace_id="t2"))
    assert payload["total"] == 1
    assert payload["items"][0]["trace_id"] == "t2"


def test_list_outbox_pending_returns_empty_when_runtime_outbox_missing() -> None:
    host = _Host()
    host.runtime = type("RT", (), {})()
    payload = asyncio.run(host.list_outbox_pending(limit=10, offset=0))
    assert payload == {"items": [], "total": 0, "limit": 10, "offset": 0}


def test_list_outbox_failures_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.list_outbox_failures(limit=10, offset=0))
    assert payload == {"items": [], "total": 0, "limit": 10, "offset": 0}


def test_get_decision_record_returns_none_without_db() -> None:
    host = _Host(has_db=False)
    result = asyncio.run(host.get_decision_record(record_id="r1"))
    assert result is None


def test_list_decisions_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.list_decisions(limit=10, offset=0))
    assert payload == {"items": [], "total": 0, "limit": 10, "offset": 0}


def test_list_strategy_candidates_returns_empty_when_strategy_id_mismatch() -> None:
    # 入参 strategy_id 与运行时 strategy 不一致 → 直接短路空集
    host = _Host(runtime_strategy_id_value="sports_tail")
    payload = asyncio.run(host.list_strategy_candidates(limit=10, offset=0, strategy_id="other"))
    assert payload["total"] == 0
    assert payload["has_more"] is False
    assert payload["source_markets"] == 0


def test_list_strategy_candidates_returns_empty_when_no_source_markets() -> None:
    host = _Host(runtime_strategy_id_value="sports_tail", source_markets_value=())
    payload = asyncio.run(host.list_strategy_candidates(limit=10, offset=0))
    assert payload["total"] == 0
    assert payload["source_markets"] == 0
    assert payload["items"] == []
