"""trace_id 字段端到端契约测试。

之前 5 个写操作 endpoint 后端 BaseModel 没有 trace_id 字段——前端 confirmAction
生成的 trace_id 拼到 reason 末尾兜底。commit 036730e 已加 trace_id 字段；本测试
验证：
- 5 个 Request 模型接受 trace_id 字段并透传到 service 调用
- 不传 trace_id 时 None 透传（service 端自己 uuid4 生成）
- 长度上限 128 / 短于 1 拒绝（防 DoS）

5 endpoints:
- POST /markets/pause / resume (PauseMarketRequest / ResumeMarketRequest)
- POST /operations/pause-trading / resume-trading (PauseTradingRequest / ResumeTradingRequest)
- PUT /parameters/{scope}/{key} (SetParameterRequest)
- DELETE /parameters/{scope}/{key} (ClearParameterRequest)
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service, get_runtime
from polymarket_trader.api.routes.markets import router as markets_router
from polymarket_trader.api.routes.operations import router as operations_router
from polymarket_trader.api.routes.parameters import router as parameters_router


class _RecordingAdminService:
    """admin_service stub：记录 pause/resume 调用的 trace_id 参数。"""

    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}

    async def pause_market_manual(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["pause_market"] = kwargs
        return {"status": "ok", "trace_id": kwargs.get("trace_id") or "auto-generated"}

    async def resume_market_manual(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["resume_market"] = kwargs
        return {"status": "ok"}

    async def pause_trading(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["pause_trading"] = kwargs
        return {"status": "ok", "trace_id": kwargs.get("trace_id") or "auto-generated"}

    async def resume_trading(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["resume_trading"] = kwargs
        return {"status": "ok"}


class _RecordingParameterStore:
    def __init__(self) -> None:
        self.set_calls: list[dict[str, Any]] = []
        self.clear_calls: list[dict[str, Any]] = []

    async def set(self, **kwargs: Any) -> dict[str, Any]:
        self.set_calls.append(kwargs)
        return {
            "scope": kwargs["scope"],
            "key": kwargs["key"],
            "value": kwargs.get("value"),
            "operator": kwargs.get("operator", "agent"),
        }

    async def clear(self, **kwargs: Any) -> dict[str, Any]:
        self.clear_calls.append(kwargs)
        return {
            "scope": kwargs["scope"],
            "key": kwargs["key"],
            "cleared": True,
            "operator": kwargs.get("operator", "agent"),
        }

    # parameters route 调 registry_payload / snapshot——本测试不用，但 Depends 不能抛
    def registry_payload(self) -> list:  # pragma: no cover - unused branch
        return []

    def snapshot(self) -> list:  # pragma: no cover - unused branch
        return []


class _RecordingRuntime:
    def __init__(self, parameter_store: _RecordingParameterStore) -> None:
        self.parameter_store = parameter_store


@pytest.fixture()
def http_client() -> tuple[TestClient, _RecordingAdminService, _RecordingParameterStore]:
    admin = _RecordingAdminService()
    param_store = _RecordingParameterStore()
    runtime = _RecordingRuntime(param_store)
    app = FastAPI()
    app.include_router(markets_router)
    app.include_router(operations_router)
    app.include_router(parameters_router)
    app.dependency_overrides[get_admin_service] = lambda: admin
    # parameters route 的 _get_param_store 直接调 get_runtime(request)，不走 Depends；
    # 必须用 app.state.runtime 而非 dependency_overrides 注入。
    app.state.runtime = runtime
    app.dependency_overrides[get_runtime] = lambda: runtime
    return TestClient(app), admin, param_store


# ============================================================
# 5 endpoint × {接受 trace_id, 缺省, 长度校验}
# ============================================================

_TRACE_ID = "manual-abc123def456"


def test_pause_market_accepts_trace_id(
    http_client: tuple[TestClient, _RecordingAdminService, _RecordingParameterStore],
) -> None:
    client, admin, _ = http_client
    response = client.post(
        "/markets/pause",
        json={
            "condition_id": "0x" + "a" * 64,
            "reason": "stale_orderbook",
            "operator": "ops",
            "trace_id": _TRACE_ID,
        },
    )
    assert response.status_code == 200, response.text
    assert admin.calls["pause_market"]["trace_id"] == _TRACE_ID


def test_pause_market_works_without_trace_id(
    http_client: tuple[TestClient, _RecordingAdminService, _RecordingParameterStore],
) -> None:
    """缺省 trace_id 时 None 透传——service 自己生成兜底。"""
    client, admin, _ = http_client
    response = client.post(
        "/markets/pause",
        json={
            "condition_id": "0x" + "a" * 64,
            "reason": "stale_orderbook",
            "operator": "ops",
        },
    )
    assert response.status_code == 200
    assert admin.calls["pause_market"]["trace_id"] is None


def test_resume_market_accepts_trace_id(
    http_client: tuple[TestClient, _RecordingAdminService, _RecordingParameterStore],
) -> None:
    client, admin, _ = http_client
    response = client.post(
        "/markets/resume",
        json={"condition_id": "0x" + "b" * 64, "operator": "ops", "trace_id": _TRACE_ID},
    )
    assert response.status_code == 200
    assert admin.calls["resume_market"]["trace_id"] == _TRACE_ID


def test_pause_trading_accepts_trace_id(
    http_client: tuple[TestClient, _RecordingAdminService, _RecordingParameterStore],
) -> None:
    client, admin, _ = http_client
    response = client.post(
        "/operations/pause-trading",
        json={"reason": "manual_drill", "operator": "ops", "trace_id": _TRACE_ID},
    )
    assert response.status_code == 200
    assert admin.calls["pause_trading"]["trace_id"] == _TRACE_ID


def test_resume_trading_accepts_trace_id(
    http_client: tuple[TestClient, _RecordingAdminService, _RecordingParameterStore],
) -> None:
    client, admin, _ = http_client
    response = client.post(
        "/operations/resume-trading",
        json={"operator": "ops", "trace_id": _TRACE_ID},
    )
    assert response.status_code == 200
    assert admin.calls["resume_trading"]["trace_id"] == _TRACE_ID


def test_parameter_set_accepts_trace_id(
    http_client: tuple[TestClient, _RecordingAdminService, _RecordingParameterStore],
) -> None:
    client, _, param_store = http_client
    response = client.put(
        "/parameters/strategy/tail_outright_min_edge_bps",
        json={"value": 500, "operator": "agent", "reason": "exploration", "trace_id": _TRACE_ID},
    )
    assert response.status_code == 200, response.text
    assert param_store.set_calls[0]["trace_id"] == _TRACE_ID


def test_parameter_clear_accepts_trace_id(
    http_client: tuple[TestClient, _RecordingAdminService, _RecordingParameterStore],
) -> None:
    client, _, param_store = http_client
    response = client.request(
        "DELETE",
        "/parameters/strategy/tail_outright_min_edge_bps",
        json={"operator": "agent", "reason": "revert", "trace_id": _TRACE_ID},
    )
    assert response.status_code == 200
    assert param_store.clear_calls[0]["trace_id"] == _TRACE_ID


# ============================================================
# 字段长度校验：min_length=1 / max_length=128
# ============================================================


def test_pause_trading_rejects_oversized_trace_id(
    http_client: tuple[TestClient, _RecordingAdminService, _RecordingParameterStore],
) -> None:
    """trace_id 超过 128 字符 → 422（防 DoS / 存储溢出）。"""
    client, _, _ = http_client
    response = client.post(
        "/operations/pause-trading",
        json={"reason": "x", "operator": "ops", "trace_id": "z" * 129},
    )
    assert response.status_code == 422


def test_pause_trading_rejects_empty_trace_id(
    http_client: tuple[TestClient, _RecordingAdminService, _RecordingParameterStore],
) -> None:
    """trace_id 空串 → 422（min_length=1）；明确传 None 才是"未指定"。"""
    client, _, _ = http_client
    response = client.post(
        "/operations/pause-trading",
        json={"reason": "x", "operator": "ops", "trace_id": ""},
    )
    assert response.status_code == 422
