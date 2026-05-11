"""新增字段守门测试（F-5 / F-6 / F-7 / F-8）。

每个 case 都是黑盒：从 HTTP 入口送入非法值，断言 4xx + 字段级 detail。
service 不被实际调用——dependency_override 用 noop sentinel 验证守门发生在
schema 层而非业务层。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.candidates import router as candidates_router
from polymarket_trader.api.routes.operations import router as operations_router


class _NoopAdminService:
    runtime = None  # virtual_paper_trade 直接读 service.runtime

    async def upsert_live_state(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "called": True, "kwargs_keys": list(kwargs)}

    async def reconcile(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "ids": len(kwargs.get("condition_ids", ()))}

    async def run_parameter_sweep(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "called": True}


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(operations_router)
    app.include_router(candidates_router)
    app.dependency_overrides[get_admin_service] = lambda: _NoopAdminService()
    return TestClient(app)


# === F-7: per_decision_usdc 下限 0.01 ===

def test_F7_per_decision_usdc_below_one_cent_rejected(client: TestClient) -> None:
    response = client.post(
        "/operations/parameter-sweep",
        json={
            "candidates": {"tail_outright_min_edge_bps": [100]},
            "per_decision_usdc": 1e-9,
            "decision_limit": 1,
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert "per_decision_usdc" in json.dumps(body)


def test_F7_per_decision_usdc_at_one_cent_accepted(client: TestClient) -> None:
    response = client.post(
        "/operations/parameter-sweep",
        json={
            "candidates": {"tail_outright_min_edge_bps": [100]},
            "per_decision_usdc": 0.01,
            "decision_limit": 1,
        },
    )
    assert response.status_code == 200


# === F-6: reconcile.condition_ids max_length=200 ===

def test_F6_reconcile_201_ids_rejected(client: TestClient) -> None:
    response = client.post(
        "/operations/reconcile",
        json={"condition_ids": [f"0x{i:064x}" for i in range(201)]},
    )
    assert response.status_code == 422
    body = response.json()
    # pydantic v2 reports max_length violation under loc=["body","condition_ids"]
    assert "condition_ids" in json.dumps(body)


def test_F6_reconcile_200_ids_accepted(client: TestClient) -> None:
    response = client.post(
        "/operations/reconcile",
        json={"condition_ids": [f"0x{i:064x}" for i in range(200)]},
    )
    assert response.status_code == 200


# === F-8: virtual-paper-trade ID 字段格式 ===

def test_F8_virtual_paper_trade_rejects_malformed_token_id(client: TestClient) -> None:
    response = client.post(
        "/operations/virtual-paper-trade",
        json={"token_id": "!@#$%^&*()", "condition_id": "0x" + "a" * 64},
    )
    assert response.status_code == 422


def test_F8_virtual_paper_trade_rejects_malformed_condition_id(client: TestClient) -> None:
    response = client.post(
        "/operations/virtual-paper-trade",
        json={"condition_id": "0xnothex_definitelynot_64_chars"},
    )
    assert response.status_code == 422


def test_F8_virtual_paper_trade_accepts_well_formed_ids(client: TestClient) -> None:
    response = client.post(
        "/operations/virtual-paper-trade",
        json={
            "condition_id": "0x" + "a" * 64,
            "token_id": "49500299856831034491021962156746701298730459370557900271970866855042624695770",
        },
    )
    # 即使 ID 格式合法但 runtime 没注册（test 用 noop service），路由层应至少 200
    # （body 由 service 层决定）；这里仅断言守门没误拦合法格式。
    assert response.status_code == 200


def test_F8_virtual_paper_trade_accepts_no_explicit_request(client: TestClient) -> None:
    response = client.post("/operations/virtual-paper-trade", json={})
    assert response.status_code == 200


# === F-5: /candidates/live-states body size limit ===

def test_F5_live_states_rejects_oversize_payload(client: TestClient) -> None:
    big_payload = {"payload": {"data": "A" * (300 * 1024)}, "condition_id": "0x" + "a" * 64}
    response = client.post("/candidates/live-states", json=big_payload)
    assert response.status_code == 413
    assert "too large" in response.json()["detail"]


def test_F5_live_states_accepts_normal_payload(client: TestClient) -> None:
    response = client.post(
        "/candidates/live-states",
        json={"payload": {"score": "1-0"}, "condition_id": "0x" + "a" * 64},
    )
    assert response.status_code == 200
