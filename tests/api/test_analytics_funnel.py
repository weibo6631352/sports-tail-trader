"""Analytics 漏斗：service 层 fake-DAO 单元测试，always-on。

集成测试用 ``@pytest.mark.pg`` 标记，仅 ``pytest --pg`` 时执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.routes.analytics import (
    DEFAULT_WINDOW_MS,
    router,
)
from polymarket_trader.app.analytics_service import AnalyticsService


@dataclass
class FakeAnalyticsDAO:
    funnel_response: dict[str, int] = field(default_factory=dict)
    rejection_response: tuple[int, list[Mapping[str, Any]]] = (0, [])
    execution_response: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def fetch_funnel_counts(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append({"method": "funnel", **kwargs})
        return dict(self.funnel_response)

    async def fetch_rejection_reasons(self, **kwargs: Any) -> tuple[int, list[Mapping[str, Any]]]:
        self.calls.append({"method": "rejections", **kwargs})
        return self.rejection_response

    async def fetch_execution_quality(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "execution", **kwargs})
        return dict(self.execution_response)


def _build_app(service: AnalyticsService) -> FastAPI:
    app = FastAPI()
    app.state.analytics_service = service
    app.include_router(router)
    return app


def _fixed_now() -> datetime:
    return datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


async def test_funnel_returns_all_stages_with_zero_default() -> None:
    dao = FakeAnalyticsDAO(funnel_response={"market_discovered": 100, "order_submitted": 5})
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    result = await service.funnel(window_ms=DEFAULT_WINDOW_MS)
    names = [s["name"] for s in result["stages"]]
    assert names == [
        "market_discovered",
        "market_filtered_in",
        "market_filtered_out",
        "risk_check_passed",
        "order_submitted",
        "fill_recorded",
    ]
    counts = {s["name"]: s["count"] for s in result["stages"]}
    assert counts["market_discovered"] == 100
    assert counts["order_submitted"] == 5
    assert counts["market_filtered_in"] == 0
    assert result["window_ms"] == DEFAULT_WINDOW_MS
    assert result["filters"] == {"league": None, "market_type": None, "strategy_id": None}


async def test_funnel_passes_league_filter_to_dao() -> None:
    dao = FakeAnalyticsDAO(funnel_response={})
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    await service.funnel(window_ms=3_600_000, league="NBA", market_type="moneyline")
    assert dao.calls[0]["league"] == "NBA"
    assert dao.calls[0]["market_type"] == "moneyline"
    # 窗口起止应严格对齐入参
    window_ms = int(
        (dao.calls[0]["window_end"] - dao.calls[0]["window_start"]).total_seconds() * 1000
    )
    assert window_ms == 3_600_000


async def test_funnel_window_end_uses_explicit_end_ms() -> None:
    dao = FakeAnalyticsDAO(funnel_response={})
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    end_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    await service.funnel(window_ms=86_400_000, end_ms=end_ms)
    assert dao.calls[0]["window_end"] == datetime(2026, 1, 1, tzinfo=timezone.utc)


async def test_funnel_invalid_window_rejected() -> None:
    dao = FakeAnalyticsDAO(funnel_response={})
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    with pytest.raises(ValueError):
        await service.funnel(window_ms=0)


def test_funnel_route_returns_default_window_and_filters() -> None:
    dao = FakeAnalyticsDAO(funnel_response={"market_discovered": 7})
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    app = _build_app(service)
    with TestClient(app) as client:
        response = client.get("/analytics/funnel")
        assert response.status_code == 200
        body = response.json()
        assert body["window_ms"] == DEFAULT_WINDOW_MS
        assert {s["name"] for s in body["stages"]} == {
            "market_discovered",
            "market_filtered_in",
            "market_filtered_out",
            "risk_check_passed",
            "order_submitted",
            "fill_recorded",
        }
        assert body["filters"] == {"league": None, "market_type": None, "strategy_id": None}


def test_funnel_route_propagates_strategy_id() -> None:
    """``?strategy_id=foo`` 应一路透传到 DAO 调用的 strategy_id 入参。"""

    dao = FakeAnalyticsDAO(funnel_response={"market_discovered": 1})
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    app = _build_app(service)
    with TestClient(app) as client:
        response = client.get(
            "/analytics/funnel",
            params={"strategy_id": "sports_tail"},
        )
        assert response.status_code == 200
        assert response.json()["filters"]["strategy_id"] == "sports_tail"
    assert dao.calls[0]["strategy_id"] == "sports_tail"


def test_funnel_route_propagates_league_and_market_type() -> None:
    dao = FakeAnalyticsDAO(funnel_response={})
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    app = _build_app(service)
    with TestClient(app) as client:
        response = client.get(
            "/analytics/funnel",
            params={"league": "NBA", "market_type": "moneyline", "window_ms": 60_000},
        )
        assert response.status_code == 200
        assert response.json()["filters"] == {"league": "NBA", "market_type": "moneyline", "strategy_id": None}
        assert dao.calls[0]["league"] == "NBA"
        assert dao.calls[0]["market_type"] == "moneyline"


def test_funnel_route_rejects_negative_window() -> None:
    dao = FakeAnalyticsDAO(funnel_response={})
    service = AnalyticsService(dao=dao, now_provider=_fixed_now)
    app = _build_app(service)
    with TestClient(app) as client:
        response = client.get("/analytics/funnel", params={"window_ms": 0})
        assert response.status_code == 422


# --------- Integration test (--pg) ---------


@pytest.mark.pg
async def test_funnel_integration_real_postgres(pg_session_factory: Any) -> None:
    """跑真 Postgres：插入审计事件 → 漏斗计数符合预期，包含 league 过滤。"""

    from polymarket_trader.infra.db.analytics_queries import fetch_funnel_counts
    from polymarket_trader.infra.db.models import AuditEventModel, MarketModel

    league = "NBA"
    other_league = "NFL"
    base = datetime(2026, 5, 10, tzinfo=timezone.utc)

    async with pg_session_factory() as session:
        # 清理目标表，避免和其他用例污染。
        from sqlalchemy import text as _t

        await session.execute(_t("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        await session.execute(_t("TRUNCATE markets RESTART IDENTITY CASCADE"))
        await session.commit()

        nba_market = MarketModel(
            condition_id="cond_nba_1",
            market_slug="nba-game-1",
            token_ids=["t1"],
            outcomes=[{"token_id": "t1", "outcome": "Yes"}],
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            neg_risk=False,
            category=league,
            tags=[league, "moneyline"],
            matched_keywords=[],
            trading_status="active",
        )
        nfl_market = MarketModel(
            condition_id="cond_nfl_1",
            market_slug="nfl-game-1",
            token_ids=["t2"],
            outcomes=[{"token_id": "t2", "outcome": "Yes"}],
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            neg_risk=False,
            category=other_league,
            tags=[other_league],
            matched_keywords=[],
            trading_status="active",
        )
        session.add_all([nba_market, nfl_market])
        await session.flush()

        def _audit(event_title: str, condition_id: str, offset_seconds: int = 0) -> AuditEventModel:
            return AuditEventModel(
                event_id=uuid4().hex,
                trace_id=uuid4().hex,
                event_title=event_title,
                condition_id=condition_id,
                payload={},
                created_at=base.replace(microsecond=offset_seconds),
            )

        session.add_all(
            [
                _audit("market_discovered", "cond_nba_1", 1),
                _audit("market_discovered", "cond_nba_1", 2),
                _audit("market_filtered_in", "cond_nba_1", 3),
                _audit("order_submitted", "cond_nba_1", 4),
                _audit("market_discovered", "cond_nfl_1", 5),
                _audit("order_rejected", "cond_nba_1", 6),
            ]
        )
        await session.commit()

        counts_all = await fetch_funnel_counts(
            session,
            window_start=base - __import__("datetime").timedelta(hours=1),
            window_end=base + __import__("datetime").timedelta(hours=1),
            league=None,
            market_type=None,
        )
        assert counts_all["market_discovered"] == 3
        assert counts_all["market_filtered_in"] == 1
        assert counts_all["order_submitted"] == 1

        counts_nba = await fetch_funnel_counts(
            session,
            window_start=base - __import__("datetime").timedelta(hours=1),
            window_end=base + __import__("datetime").timedelta(hours=1),
            league="NBA",
            market_type=None,
        )
        assert counts_nba["market_discovered"] == 2
        assert counts_nba["order_submitted"] == 1
        # NFL discover 不应被算到 NBA
        assert counts_nba.get("market_filtered_in", 0) == 1
