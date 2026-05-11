"""C-3 admin token middleware + H-3 rate limiter route-level coverage."""
from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from polymarket_trader.api.app import create_app
from polymarket_trader.api.rate_limit import rate_limit
from polymarket_trader.config import Settings


class _DummyRuntime:
    """create_app lifespan 期望 runtime 有最少几个属性——给它最小可用 stub。"""

    def __init__(self) -> None:
        self.admin_service = None
        self.db_session_factory = None

    async def aclose(self) -> None:
        return None


class _DummyAdminService:
    def __init__(self) -> None:
        self.runtime: Any | None = None

    def bind_runtime(self, runtime: Any) -> "_DummyAdminService":
        self.runtime = runtime
        return self

    async def upsert_live_state(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok"}

    def health_snapshot(self) -> dict[str, str]:
        return {"status": "ok"}


def _make_app(*, admin_token: str | None) -> FastAPI:
    settings = Settings(
        admin_api_token=SecretStr(admin_token) if admin_token else None,
        expose_openapi_docs=False,
        portfolio_budget_usdc="10",
        max_order_usdc="10",
        max_market_usdc="10",
        max_total_usdc="10",
        max_open_orders=10,
        wallet_private_key=SecretStr("dummy"),
    )
    runtime = _DummyRuntime()
    admin = _DummyAdminService()
    return create_app(runtime=runtime, admin_service=admin, settings=settings)


# === C-3 鉴权 ===

def test_admin_token_required_when_configured() -> None:
    app = _make_app(admin_token="secret-prod-token")
    with TestClient(app) as client:
        # 没带 token 的写接口被 401
        resp = client.post(
            "/candidates/live-states",
            json={"payload": {"x": 1}, "condition_id": "0x" + "a" * 64},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "admin_token_required"

        # /health 是豁免路径，不需要 token
        resp_health = client.get("/health")
        assert resp_health.status_code == 200


def test_admin_token_accepts_when_header_matches() -> None:
    app = _make_app(admin_token="secret-prod-token")
    with TestClient(app) as client:
        resp = client.post(
            "/candidates/live-states",
            headers={"X-Admin-Token": "secret-prod-token"},
            json={"payload": {"x": 1}, "condition_id": "0x" + "a" * 64},
        )
        # 通过 token；service 是 _DummyAdminService.upsert_live_state 返回 {"status":"ok"}
        assert resp.status_code == 200


def test_admin_token_disabled_allows_anonymous() -> None:
    app = _make_app(admin_token=None)
    with TestClient(app) as client:
        resp = client.post(
            "/candidates/live-states",
            json={"payload": {"x": 1}, "condition_id": "0x" + "a" * 64},
        )
        # token 未配置 → 不强制；service stub 返回 200
        assert resp.status_code == 200


def test_openapi_docs_hidden_when_disabled() -> None:
    app = _make_app(admin_token=None)
    with TestClient(app) as client:
        # expose_openapi_docs=False ⇒ /docs / /openapi.json 都 404
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        # /health 仍可访问，确认 app 在跑
        assert client.get("/health").status_code == 200


# === H-3 rate limit ===

def test_rate_limit_blocks_after_burst() -> None:
    # 隔离测试：直接用 dependency factory，模拟单一 endpoint qps=1 burst=2
    from fastapi import Depends

    app = FastAPI()

    @app.get("/expensive")
    def handler(_: None = Depends(rate_limit(endpoint="test_burst", qps=1.0, burst=2))) -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as client:
        # 桶容量=2：前 2 个请求成功
        assert client.get("/expensive").status_code == 200
        assert client.get("/expensive").status_code == 200
        # 第 3 个超 burst，429 + Retry-After
        resp = client.get("/expensive")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert resp.json()["detail"]["code"] == "rate_limit_exceeded"


def test_rate_limit_refills_over_time() -> None:
    from fastapi import Depends

    app = FastAPI()

    @app.get("/refill")
    def handler(_: None = Depends(rate_limit(endpoint="test_refill", qps=10.0, burst=1))) -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as client:
        assert client.get("/refill").status_code == 200
        assert client.get("/refill").status_code == 429
        # 100ms 应该 refill 1 token (qps=10)
        time.sleep(0.12)
        assert client.get("/refill").status_code == 200
