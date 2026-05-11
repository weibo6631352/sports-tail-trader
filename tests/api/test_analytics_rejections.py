"""Analytics rejections：top-N 拒绝原因。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.routes.analytics import DEFAULT_WINDOW_MS, router
from polymarket_trader.app.analytics_service import AnalyticsService


@dataclass
class FakeDAO:
    rejection_response: tuple[int, list[Mapping[str, Any]]] = (0, [])
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def fetch_funnel_counts(self, **kwargs: Any) -> dict[str, int]:
        return {}

    async def fetch_rejection_reasons(self, **kwargs: Any) -> tuple[int, list[Mapping[str, Any]]]:
        self.calls.append(kwargs)
        return self.rejection_response

    async def fetch_execution_quality(self, **kwargs: Any) -> dict[str, Any]:
        return {}


def _fixed_now() -> datetime:
    return datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


async def test_rejections_computes_pct_against_total() -> None:
    dao = FakeDAO(
        rejection_response=(
            100,
            [
                {"key": "insufficient_balance", "count": 60},
                {"key": "price_band_violation", "count": 30},
                {"key": "<empty>", "count": 10},
            ],
        )
    )
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    result = await service.rejections(window_ms=DEFAULT_WINDOW_MS)
    assert result["total"] == 100
    assert result["top"][0] == {"key": "insufficient_balance", "count": 60, "pct": 60.0}
    assert result["top"][1]["pct"] == 30.0
    assert result["top"][2]["pct"] == 10.0


async def test_rejections_zero_total_pct_is_zero() -> None:
    dao = FakeDAO(rejection_response=(0, []))
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    result = await service.rejections(window_ms=DEFAULT_WINDOW_MS)
    assert result["total"] == 0
    assert result["top"] == []


async def test_rejections_passes_limit_20_to_dao() -> None:
    dao = FakeDAO(rejection_response=(0, []))
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    await service.rejections(window_ms=DEFAULT_WINDOW_MS, limit=20)
    assert dao.calls[0]["limit"] == 20


def test_rejections_route_default_limit_is_20() -> None:
    dao = FakeDAO(rejection_response=(0, []))
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    app = FastAPI()
    app.state.analytics_service = service
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/analytics/rejections", params={"league": "NBA"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["top"] == []
        assert body["filters"]["league"] == "NBA"
        assert dao.calls[0]["limit"] == 20
        assert dao.calls[0]["league"] == "NBA"


# --------- Integration (--pg) ---------


@pytest.mark.pg
async def test_rejections_integration_top_reasons(pg_session_factory: Any) -> None:
    from sqlalchemy import text as _t

    from polymarket_trader.infra.db.analytics_queries import fetch_rejection_reasons
    from polymarket_trader.infra.db.models import AuditEventModel, MarketModel

    base = datetime(2026, 5, 10, tzinfo=timezone.utc)
    async with pg_session_factory() as session:
        await session.execute(_t("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        await session.execute(_t("TRUNCATE markets RESTART IDENTITY CASCADE"))
        await session.commit()

        session.add(
            MarketModel(
                condition_id="cond_x",
                market_slug="slug-x",
                token_ids=["tx"],
                outcomes=[{"token_id": "tx", "outcome": "Yes"}],
                tick_size=Decimal("0.01"),
                min_order_size=Decimal("1"),
                neg_risk=False,
                category="NBA",
                tags=["NBA"],
                matched_keywords=[],
                trading_status="active",
            )
        )
        await session.flush()

        for i in range(5):
            session.add(
                AuditEventModel(
                    event_id=uuid4().hex,
                    trace_id=uuid4().hex,
                    event_title="order_rejected",
                    condition_id="cond_x",
                    reason="insufficient_balance",
                    payload={},
                    created_at=base + timedelta(seconds=i),
                )
            )
        for i in range(2):
            session.add(
                AuditEventModel(
                    event_id=uuid4().hex,
                    trace_id=uuid4().hex,
                    event_title="risk_check_failed",
                    condition_id="cond_x",
                    reason="price_band_violation",
                    payload={},
                    created_at=base + timedelta(seconds=10 + i),
                )
            )
        session.add(
            AuditEventModel(
                event_id=uuid4().hex,
                trace_id=uuid4().hex,
                event_title="market_filtered_out",
                condition_id="cond_x",
                reason="",
                payload={},
                created_at=base + timedelta(seconds=20),
            )
        )
        await session.commit()

        total, rows = await fetch_rejection_reasons(
            session,
            window_start=base - timedelta(hours=1),
            window_end=base + timedelta(hours=1),
            league=None,
            market_type=None,
            limit=20,
        )
        assert total == 8
        keys = [r["key"] for r in rows]
        assert keys[0] == "insufficient_balance"
        assert "price_band_violation" in keys
        assert "<empty>" in keys
