"""Streaming export endpoint tests.

走 FastAPI 内嵌 ASGI 客户端，注入一个假的 db_session_factory 和被打过桩的
仓储 stream 方法，验证：
- CSV/JSONL 体格式、列序与 SA 声明序一致
- 资源/格式校验返回 404/400
- limit 超过上限被夹到 100000 并带 warning header
- JSONL Decimal 序列化保留精度（字符串形式）
- TimeRange 透传：路由层把 since/until 构造的 TimeRange 透传给 stream fn

不连真实数据库，也不触发风控/交易主链路。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from polymarket_trader.api.routes import exports as exports_module
from polymarket_trader.api.routes.exports import MAX_LIMIT, router
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import AuditEventModel, FillModel, OrderModel


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _fake_session_factory() -> _FakeSession:
    return _FakeSession()


def _seed_orders() -> list[OrderModel]:
    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    rows: list[OrderModel] = []
    for index in range(5):
        row = OrderModel(
            order_key=f"order-key-{index}",
            trace_id=f"trace-{index}",
            condition_id=f"cond-{index}",
            token_id=f"token-{index}",
            market_slug=f"slug-{index}",
            side="BUY" if index % 2 == 0 else "SELL",
            order_type="FAK",
            price=Decimal("0.7123456789012345678"),
            amount_usdc=Decimal("5.000000000000000001"),
            size_shares=Decimal("7.040000000000000001"),
            filled_shares=Decimal("0"),
            remaining_shares=Decimal("7.04"),
            notional_usdc=Decimal("5"),
            order_id=f"exchange-{index}",
            trade_id=None,
            status="submitted",
            idempotency_key=f"idem-{index}",
            reason="seed",
            post_only=False,
            raw_payload={"k": "v", "i": index},
            created_at=base,
            updated_at=base,
        )
        rows.append(row)
    return rows


def _seed_fills() -> list[FillModel]:
    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    rows: list[FillModel] = []
    for index in range(5):
        row = FillModel(
            event_id=f"event-{index}",
            trace_id=f"trace-{index}",
            event_type="fill",
            condition_id=f"cond-{index}",
            token_id=f"token-{index}",
            market_slug=f"slug-{index}",
            order_id=f"order-{index}",
            trade_id=f"trade-{index}",
            side="BUY",
            price=Decimal("0.7"),
            size=Decimal("1.0"),
            notional_usdc=Decimal("0.7"),
            status="confirmed",
            confirmed_at=base,
            raw_payload={"i": index},
            created_at=base,
            updated_at=base,
        )
        rows.append(row)
    return rows


def _seed_audit_events() -> list[AuditEventModel]:
    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    rows: list[AuditEventModel] = []
    for index in range(5):
        row = AuditEventModel(
            event_id=f"event-{index}",
            trace_id=f"trace-{index}",
            event_title="order_submitted",
            market_slug=f"slug-{index}",
            event_slug=f"event-slug-{index}",
            condition_id=f"cond-{index}",
            token_id=f"token-{index}",
            outcome="YES",
            side="BUY",
            order_type="FAK",
            price=Decimal("0.7"),
            size=Decimal("1.0"),
            notional_usdc=Decimal("0.7"),
            order_id=f"order-{index}",
            trade_id=None,
            tx_hash=None,
            status="ok",
            reason=None,
            raw_response=None,
            payload={"i": index},
            created_at=base,
            updated_at=base,
        )
        rows.append(row)
    return rows


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = _fake_session_factory
    app.include_router(router)

    orders = _seed_orders()
    fills = _seed_fills()
    audits = _seed_audit_events()

    captured: dict[str, Any] = {}

    _seed_by_resource: dict[str, list[Any]] = {
        "orders": orders,
        "fills": fills,
        "audit_events": audits,
    }

    async def _fake_stream_resource_rows(
        session: Any, resource: str, time_range: Any, limit: int
    ) -> AsyncIterator[Any]:
        captured[resource] = {"time_range": time_range, "limit": limit}
        for row in _seed_by_resource.get(resource, [])[:limit]:
            yield row

    monkeypatch.setattr(exports_module, "stream_resource_rows", _fake_stream_resource_rows)

    app.state.captured = captured  # type: ignore[attr-defined]
    return app


async def _get(app: FastAPI, path: str) -> tuple[int, dict[str, str], bytes]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://export-test") as client:
        response = await client.get(path)
    return response.status_code, dict(response.headers), response.content


async def test_orders_csv_streams_header_and_five_rows(app: FastAPI) -> None:
    status, headers, body = await _get(app, "/exports/orders?format=csv")
    assert status == 200
    assert headers["content-type"].startswith("text/csv")
    assert 'filename="orders__.csv"' in headers["content-disposition"]
    lines = body.decode("utf-8").splitlines()
    assert len(lines) == 6  # header + 5 rows

    expected_header = [column.name for column in OrderModel.__table__.columns]
    assert lines[0].split(",") == expected_header

    assert "0.7123456789012345678" in lines[1]


async def test_fills_jsonl_streams_five_parseable_lines(app: FastAPI) -> None:
    status, headers, body = await _get(app, "/exports/fills?format=jsonl")
    assert status == 200
    assert headers["content-type"].startswith("application/x-ndjson")
    lines = body.decode("utf-8").splitlines()
    assert len(lines) == 5

    columns = [column.name for column in FillModel.__table__.columns]
    for line in lines:
        record = json.loads(line)
        assert set(record.keys()) == set(columns)
        assert isinstance(record["price"], str)
        assert record["price"] == "0.7"


async def test_unknown_resource_returns_404(app: FastAPI) -> None:
    status, _headers, body = await _get(app, "/exports/foo?format=csv")
    assert status == 404
    assert b"unknown_resource" in body


async def test_unsupported_format_returns_400(app: FastAPI) -> None:
    status, _headers, body = await _get(app, "/exports/orders?format=xml")
    assert status == 400
    assert b"unsupported_format" in body


async def test_limit_clamped_warns_via_header(app: FastAPI) -> None:
    status, headers, _body = await _get(
        app, "/exports/orders?format=csv&limit=200000"
    )
    assert status == 200
    assert headers.get("x-export-warning") == "limit_clamped_to_100000"
    captured = app.state.captured["orders"]  # type: ignore[attr-defined]
    assert captured["limit"] == MAX_LIMIT


async def test_audit_events_csv_time_range_threaded_through(app: FastAPI) -> None:
    status, _headers, body = await _get(
        app, "/exports/audit_events?format=csv&since=1762430400000&until=1762516800000"
    )
    assert status == 200
    lines = body.decode("utf-8").splitlines()
    expected_header = [column.name for column in AuditEventModel.__table__.columns]
    assert lines[0].split(",") == expected_header
    assert len(lines) == 6
    captured = app.state.captured["audit_events"]  # type: ignore[attr-defined]
    time_range = captured["time_range"]
    assert isinstance(time_range, TimeRange)
    assert time_range.since_ms == 1762430400000
    assert time_range.until_ms == 1762516800000


async def test_since_after_until_returns_400(app: FastAPI) -> None:
    status, _headers, body = await _get(
        app, "/exports/orders?format=csv&since=2000&until=1000"
    )
    assert status == 400
    assert b"since_after_until" in body


async def test_missing_session_factory_returns_503() -> None:
    app = FastAPI()
    app.include_router(router)
    status, _headers, body = await _get(app, "/exports/orders?format=csv")
    assert status == 503
    assert b"db_session_factory_unavailable" in body
