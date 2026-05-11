"""Analytics execution-quality：延迟和滑点。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.routes.analytics import router
from polymarket_trader.app.analytics_service import AnalyticsService


@dataclass
class FakeDAO:
    execution_response: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def fetch_funnel_counts(self, **kwargs: Any) -> dict[str, int]:
        return {}

    async def fetch_rejection_reasons(self, **kwargs: Any) -> tuple[int, list[Any]]:
        return 0, []

    async def fetch_execution_quality(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return dict(self.execution_response)


def _fixed_now() -> datetime:
    return datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


async def test_execution_quality_shapes_response() -> None:
    dao = FakeDAO(
        execution_response={
            "submit_p50": 12.5,
            "submit_p95": 80.0,
            "fill_p50": 200.0,
            "fill_p95": 1500.0,
            "slip_mean": 4.5,
            "slip_p95": 22.0,
            "sample_size": 42,
        }
    )
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    result = await service.execution_quality(window_ms=86_400_000)
    assert result["submit_latency_ms"] == {"p50": 12.5, "p95": 80.0}
    assert result["fill_latency_ms"] == {"p50": 200.0, "p95": 1500.0}
    assert result["slippage_bps"] == {"mean": 4.5, "p95": 22.0}
    assert result["sample_size"] == 42


async def test_execution_quality_handles_nulls_when_empty() -> None:
    dao = FakeDAO(
        execution_response={
            "submit_p50": None,
            "submit_p95": None,
            "fill_p50": None,
            "fill_p95": None,
            "slip_mean": None,
            "slip_p95": None,
            "sample_size": 0,
        }
    )
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    result = await service.execution_quality(window_ms=86_400_000)
    assert result["submit_latency_ms"] == {"p50": None, "p95": None}
    assert result["slippage_bps"] == {"mean": None, "p95": None}
    assert result["sample_size"] == 0


def test_execution_quality_route_default() -> None:
    dao = FakeDAO(
        execution_response={
            "submit_p50": 1.0,
            "submit_p95": 2.0,
            "fill_p50": 3.0,
            "fill_p95": 4.0,
            "slip_mean": 0.5,
            "slip_p95": 1.5,
            "sample_size": 10,
        }
    )
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    app = FastAPI()
    app.state.analytics_service = service
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/analytics/execution-quality")
        assert response.status_code == 200
        body = response.json()
        assert body["submit_latency_ms"]["p50"] == 1.0
        assert body["sample_size"] == 10


# --------- Integration (--pg) ---------


@pytest.mark.pg
async def test_execution_quality_integration_fill_latency(pg_session_factory: Any) -> None:
    from sqlalchemy import text as _t

    from polymarket_trader.infra.db.analytics_queries import fetch_execution_quality
    from polymarket_trader.infra.db.models import (
        AuditEventModel,
        FillModel,
        MarketModel,
        OrderModel,
    )

    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    async with pg_session_factory() as session:
        for table in ("audit_events", "fills", "orders", "markets"):
            await session.execute(_t(f"TRUNCATE {table} RESTART IDENTITY CASCADE"))
        await session.commit()

        session.add(
            MarketModel(
                condition_id="c1",
                market_slug="slug",
                token_ids=["t1"],
                outcomes=[{"token_id": "t1", "outcome": "Yes"}],
                tick_size=Decimal("0.01"),
                min_order_size=Decimal("1"),
                neg_risk=False,
                category="NBA",
                tags=["NBA"],
                matched_keywords=[],
                trading_status="active",
            )
        )

        # filtered_in at T, order_submitted at T+50ms
        flt_id = uuid4().hex
        session.add(
            AuditEventModel(
                event_id=flt_id,
                trace_id="trace1",
                event_title="market_filtered_in",
                condition_id="c1",
                token_id="t1",
                payload={},
                created_at=base,
            )
        )
        session.add(
            AuditEventModel(
                event_id=uuid4().hex,
                trace_id="trace1",
                event_title="order_submitted",
                condition_id="c1",
                token_id="t1",
                payload={},
                created_at=base + timedelta(milliseconds=50),
            )
        )

        # Order created at T+50ms, fill confirmed at T+200ms => fill latency = 150ms
        # order price 0.50, fill price 0.52 (buy slippage = 400 bps unfavorable)
        order_id = "order_1"
        session.add(
            OrderModel(
                order_key=order_id,
                trace_id="trace1",
                condition_id="c1",
                token_id="t1",
                market_slug="slug",
                side="buy",
                order_type="gtc",
                price=Decimal("0.50"),
                filled_shares=Decimal("0"),
                order_id=order_id,
                status="open",
                reason="",
                post_only=False,
                created_at=base + timedelta(milliseconds=50),
            )
        )
        session.add(
            FillModel(
                event_id=uuid4().hex,
                trace_id="trace1",
                event_type="fill_recorded",
                condition_id="c1",
                token_id="t1",
                order_id=order_id,
                trade_id="trade1",
                side="buy",
                price=Decimal("0.52"),
                size=Decimal("10"),
                notional_usdc=Decimal("5.20"),
                status="confirmed",
                confirmed_at=base + timedelta(milliseconds=200),
            )
        )
        await session.commit()

        result = await fetch_execution_quality(
            session,
            window_start=base - timedelta(hours=1),
            window_end=base + timedelta(hours=1),
            league=None,
            market_type=None,
        )
        assert result["sample_size"] == 1
        # submit_latency = 50ms
        assert result["submit_p50"] is not None
        assert abs(result["submit_p50"] - 50.0) < 1.0
        # fill_latency = 150ms
        assert abs(result["fill_p50"] - 150.0) < 1.0
        # slippage: (0.52-0.50)/0.50 * 10000 = 400 bps
        assert abs(result["slip_mean"] - 400.0) < 0.001
