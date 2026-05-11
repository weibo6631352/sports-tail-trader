"""``GET /admin/decisions/dump`` 路由 + ``AdminService.list_decisions`` 行为。

覆盖两层语义：
1. 路由层把 ``since`` / ``until`` 转 ``TimeRange``，把布尔/字符串 query 透传给
   admin service；``since > until`` 抛 400 ``since_after_until``。
2. AdminService 层在没有 DB session factory 时退化为空集；在有 session factory
   时下沉到 ``DecisionRecordRepository.list_decisions_snapshot``，按
   trace_id / condition_id / accepted / time_range 过滤并 page。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.runtime import router as runtime_router
from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage


class _RecordingAdminService:
    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None

    async def list_decisions(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return {
            "items": [],
            "total": 0,
            "limit": kwargs.get("limit", 1000),
            "offset": kwargs.get("offset", 0),
        }

    # 路由 module 仅调用 list_decisions —— 其它方法不需要实现。
    async def runtime_snapshot(self) -> dict[str, Any]:
        return {}

    def workers_snapshot(self) -> dict[str, Any]:
        return {}

    def metrics_snapshot(self) -> dict[str, Any]:
        return {}


@pytest.fixture()
def app_with_admin() -> tuple[FastAPI, _RecordingAdminService]:
    service = _RecordingAdminService()
    app = FastAPI()
    app.include_router(runtime_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return app, service


def test_dump_default_query_uses_default_limit_and_no_time_range(
    app_with_admin: tuple[FastAPI, _RecordingAdminService],
) -> None:
    app, service = app_with_admin
    with TestClient(app) as client:
        response = client.get("/admin/decisions/dump")
    assert response.status_code == 200
    assert service.last_kwargs is not None
    assert service.last_kwargs["limit"] == 1000
    assert service.last_kwargs["offset"] == 0
    assert service.last_kwargs["time_range"] is None
    assert service.last_kwargs["accepted"] is None
    assert service.last_kwargs["trace_id"] is None
    assert service.last_kwargs["condition_id"] is None


def test_dump_passes_filters_and_builds_time_range(
    app_with_admin: tuple[FastAPI, _RecordingAdminService],
) -> None:
    app, service = app_with_admin
    with TestClient(app) as client:
        response = client.get(
            "/admin/decisions/dump",
            params={
                "limit": 25,
                "offset": 5,
                "trace_id": "trace-x",
                "condition_id": "cond-x",
                "accepted": "true",
                "since": 1_000,
                "until": 2_000,
            },
        )
    assert response.status_code == 200
    kwargs = service.last_kwargs
    assert kwargs is not None
    assert kwargs["limit"] == 25
    assert kwargs["offset"] == 5
    assert kwargs["trace_id"] == "trace-x"
    assert kwargs["condition_id"] == "cond-x"
    assert kwargs["accepted"] is True
    time_range = kwargs["time_range"]
    assert isinstance(time_range, TimeRange)
    assert (time_range.since_ms, time_range.until_ms) == (1_000, 2_000)


def test_dump_since_after_until_returns_400(
    app_with_admin: tuple[FastAPI, _RecordingAdminService],
) -> None:
    app, _service = app_with_admin
    with TestClient(app) as client:
        response = client.get(
            "/admin/decisions/dump", params={"since": 2_000, "until": 1_000}
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "since_after_until"


# --- AdminService 层 ----------------------------------------------------------


class _StubRepo:
    def __init__(self, records: tuple[DecisionRecord, ...]) -> None:
        self._records = records
        self.last_kwargs: dict[str, Any] | None = None

    async def list_decisions_snapshot(self, **kwargs: Any) -> RepositoryPage[DecisionRecord]:
        self.last_kwargs = kwargs
        items = self._records
        if kwargs.get("trace_id") is not None:
            items = tuple(r for r in items if r.trace_id == kwargs["trace_id"])
        if kwargs.get("condition_id") is not None:
            items = tuple(r for r in items if r.condition_id == kwargs["condition_id"])
        if kwargs.get("accepted") is not None:
            items = tuple(r for r in items if r.accepted is kwargs["accepted"])
        time_range = kwargs.get("time_range")
        if isinstance(time_range, TimeRange):
            items = tuple(r for r in items if time_range.contains(r.created_at))
        limit = kwargs.get("limit", 1000)
        offset = kwargs.get("offset", 0)
        sliced = items[offset : offset + limit]
        return RepositoryPage(items=sliced, total=len(items), limit=limit, offset=offset)


class _FakeRepoGroup:
    def __init__(self, decision_repo: _StubRepo) -> None:
        self.decision = decision_repo


class _FakeRuntime:
    def __init__(self, *, with_db: bool) -> None:
        # AdminService 只看 db_session_factory 是否 None；不会用真实 factory，
        # 因为我们在测试里把 _with_repositories 整体替换掉。
        self.db_session_factory = (lambda: None) if with_db else None


def _seeded_records() -> tuple[DecisionRecord, ...]:
    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    return (
        DecisionRecord(
            record_id="rid-1",
            trace_id="trace-A",
            condition_id="cond-A",
            decision_input={"market": "a"},
            decision_output={"action": "skip", "reason": "no_signal"},
            accepted=False,
            reason="no_signal",
            created_at=base,
        ),
        DecisionRecord(
            record_id="rid-2",
            trace_id="trace-B",
            condition_id="cond-A",
            decision_input={"market": "a"},
            decision_output={"action": "buy", "price": "0.5", "amount_usdc": "3"},
            accepted=True,
            reason=None,
            created_at=base,
        ),
        DecisionRecord(
            record_id="rid-3",
            trace_id="trace-C",
            condition_id="cond-B",
            decision_input={"market": "b"},
            decision_output={"action": "skip", "reason": "risk_blocked"},
            accepted=False,
            reason="risk_blocked",
            created_at=base,
        ),
    )


async def test_admin_list_decisions_without_db_returns_empty_payload() -> None:
    service = AdminService().bind_runtime(_FakeRuntime(with_db=False))
    payload = await service.list_decisions()
    assert payload["items"] == []
    assert payload["total"] == 0


async def _run_with_stub_repo(repo: _StubRepo, **kwargs: Any) -> dict[str, Any]:
    service = AdminService().bind_runtime(_FakeRuntime(with_db=True))

    async def _fake_with_repos(_self: AdminService, callback: Any) -> Any:
        # admin_query_mixin.list_decisions 只通过 repos.decision 访问 repo。
        return await callback(_FakeRepoGroup(decision_repo=repo))

    # AdminService 是 frozen dataclass —— 不能直接对实例 setattr，挂到类层；
    # 调用方式是 ``self._with_repositories(callback)``，所以 fake 也要接受 self。
    original = AdminService._with_repositories
    AdminService._with_repositories = _fake_with_repos  # type: ignore[assignment]
    try:
        return await service.list_decisions(**kwargs)
    finally:
        AdminService._with_repositories = original  # type: ignore[assignment]


async def test_admin_list_decisions_uses_repository_when_db_available() -> None:
    repo = _StubRepo(_seeded_records())
    payload = await _run_with_stub_repo(repo, condition_id="cond-A")

    assert payload["total"] == 2
    assert len(payload["items"]) == 2
    first = payload["items"][0]
    assert set(first.keys()) >= {
        "record_id",
        "trace_id",
        "condition_id",
        "decision_input",
        "decision_output",
        "accepted",
        "reason",
        "created_at",
    }
    assert {item["trace_id"] for item in payload["items"]} == {"trace-A", "trace-B"}
    assert repo.last_kwargs is not None
    assert repo.last_kwargs["condition_id"] == "cond-A"


async def test_admin_list_decisions_filters_by_accepted() -> None:
    repo = _StubRepo(_seeded_records())
    payload = await _run_with_stub_repo(repo, accepted=True)
    assert payload["total"] == 1
    assert payload["items"][0]["trace_id"] == "trace-B"
    assert payload["items"][0]["accepted"] is True
