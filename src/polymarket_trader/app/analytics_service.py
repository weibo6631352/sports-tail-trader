"""Analytics service.

只编排只读聚合查询：构造时间窗、调用 DAO、整理响应结构。
不写库、不接交易主链路、不持有运行时锁。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol

from polymarket_trader.infra.db.analytics_queries import FUNNEL_STAGES


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class AnalyticsDAO(Protocol):
    """Analytics DAO 抽象，便于 service 用 fake 注入做单元测试。"""

    async def fetch_funnel_counts(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        league: str | None,
        market_type: str | None,
    ) -> dict[str, int]: ...

    async def fetch_rejection_reasons(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        league: str | None,
        market_type: str | None,
        limit: int,
    ) -> tuple[int, list[Mapping[str, Any]]]: ...

    async def fetch_execution_quality(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        league: str | None,
        market_type: str | None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AnalyticsService:
    """读分析数据，统一窗口/参数语义。

    - 所有时间都是 UTC。
    - 入参 ``end_ms`` 缺省为 ``now()``；``window_ms`` 缺省由路由传入。
    - 不接触运行时热状态，纯 DB 查询。
    """

    dao: AnalyticsDAO
    now_provider: Callable[[], datetime] = field(default=_default_now)

    def _window(self, *, window_ms: int, end_ms: int | None) -> tuple[datetime, datetime]:
        if window_ms <= 0:
            raise ValueError("window_ms must be positive")
        if end_ms is None:
            end_dt = self.now_provider()
        else:
            end_dt = datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc)
        start_dt = datetime.fromtimestamp(
            (int(end_dt.timestamp() * 1000) - window_ms) / 1000.0, tz=timezone.utc
        )
        return start_dt, end_dt

    async def funnel(
        self,
        *,
        window_ms: int,
        end_ms: int | None = None,
        league: str | None = None,
        market_type: str | None = None,
    ) -> dict[str, Any]:
        start_dt, end_dt = self._window(window_ms=window_ms, end_ms=end_ms)
        counts = await self.dao.fetch_funnel_counts(
            window_start=start_dt,
            window_end=end_dt,
            league=league,
            market_type=market_type,
        )
        stages = [{"name": name, "count": int(counts.get(name, 0))} for name in FUNNEL_STAGES]
        return {
            "window_ms": window_ms,
            "generated_at": end_dt.isoformat(),
            "stages": stages,
            "filters": {"league": league, "market_type": market_type},
        }

    async def rejections(
        self,
        *,
        window_ms: int,
        end_ms: int | None = None,
        league: str | None = None,
        market_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        start_dt, end_dt = self._window(window_ms=window_ms, end_ms=end_ms)
        total, rows = await self.dao.fetch_rejection_reasons(
            window_start=start_dt,
            window_end=end_dt,
            league=league,
            market_type=market_type,
            limit=limit,
        )
        top: list[dict[str, Any]] = []
        for row in rows:
            count = int(row["count"])
            pct = (count / total * 100.0) if total > 0 else 0.0
            top.append({"key": str(row["key"]), "count": count, "pct": round(pct, 4)})
        return {
            "window_ms": window_ms,
            "generated_at": end_dt.isoformat(),
            "total": total,
            "top": top,
            "filters": {"league": league, "market_type": market_type},
        }

    async def execution_quality(
        self,
        *,
        window_ms: int,
        end_ms: int | None = None,
        league: str | None = None,
        market_type: str | None = None,
    ) -> dict[str, Any]:
        start_dt, end_dt = self._window(window_ms=window_ms, end_ms=end_ms)
        data = await self.dao.fetch_execution_quality(
            window_start=start_dt,
            window_end=end_dt,
            league=league,
            market_type=market_type,
        )
        return {
            "window_ms": window_ms,
            "generated_at": end_dt.isoformat(),
            "submit_latency_ms": {
                "p50": _round_or_none(data.get("submit_p50")),
                "p95": _round_or_none(data.get("submit_p95")),
            },
            "fill_latency_ms": {
                "p50": _round_or_none(data.get("fill_p50")),
                "p95": _round_or_none(data.get("fill_p95")),
            },
            "slippage_bps": {
                "mean": _round_or_none(data.get("slip_mean")),
                "p95": _round_or_none(data.get("slip_p95")),
            },
            "sample_size": int(data.get("sample_size") or 0),
            "filters": {"league": league, "market_type": market_type},
        }


def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


class SessionFactoryAnalyticsDAO:
    """生产环境 DAO：每次查询打开独立 session。

    与 AdminService._with_repositories 同模式，只读会话用完即释放，
    不长期持有连接，不污染交易主链路。
    """

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def _run(self, callback: Callable[[Any], Awaitable[Any]]) -> Any:
        async with self._session_factory() as session:
            return await callback(session)

    async def fetch_funnel_counts(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        league: str | None,
        market_type: str | None,
    ) -> dict[str, int]:
        from polymarket_trader.infra.db.analytics_queries import fetch_funnel_counts

        async def _do(session: Any) -> dict[str, int]:
            return await fetch_funnel_counts(
                session,
                window_start=window_start,
                window_end=window_end,
                league=league,
                market_type=market_type,
            )

        return await self._run(_do)

    async def fetch_rejection_reasons(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        league: str | None,
        market_type: str | None,
        limit: int,
    ) -> tuple[int, list[Mapping[str, Any]]]:
        from polymarket_trader.infra.db.analytics_queries import fetch_rejection_reasons

        async def _do(session: Any) -> tuple[int, list[Mapping[str, Any]]]:
            return await fetch_rejection_reasons(
                session,
                window_start=window_start,
                window_end=window_end,
                league=league,
                market_type=market_type,
                limit=limit,
            )

        return await self._run(_do)

    async def fetch_execution_quality(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        league: str | None,
        market_type: str | None,
    ) -> dict[str, Any]:
        from polymarket_trader.infra.db.analytics_queries import fetch_execution_quality

        async def _do(session: Any) -> dict[str, Any]:
            return await fetch_execution_quality(
                session,
                window_start=window_start,
                window_end=window_end,
                league=league,
                market_type=market_type,
            )

        return await self._run(_do)


__all__ = ("AnalyticsDAO", "AnalyticsService", "SessionFactoryAnalyticsDAO")
