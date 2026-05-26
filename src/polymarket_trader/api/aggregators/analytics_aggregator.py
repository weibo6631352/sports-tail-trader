"""AnalyticsAggregator —— 报表类分析查询（DB-only）。

替代 `app/admin_query/analytics.py` 全部方法 + admin_query/trading.py 的
risk_rejections 相关 method（语义上属于 analytics 类）。

按 docs/新架构方案.md §12.2 审计查询类（走 DB）。

# Endpoint 对应

| Endpoint | 方法 |
|---|---|
| `GET /analytics/edge-realization` | `edge_realization_snapshot(...)` |
| `GET /portfolio/pnl-breakdown` | `pnl_breakdown_snapshot(...)` |
| `GET /analytics/missed-opportunities` | `missed_opportunities_snapshot(...)` |
| `GET /analytics/calibration` | `calibration_snapshot(...)` |
| `GET /analytics/risk-rejections` | `list_risk_rejections(...)` |
| `GET /analytics/risk-rejections/aggregate` | `aggregate_risk_rejections(...)` |
| `GET /runtime/latency-percentiles` | `latency_percentiles_snapshot(...)` |
| `GET /portfolio/equity-curve` | `portfolio_equity_curve(...)` |
| `GET /portfolio/risk-metrics` | `portfolio_risk_metrics(...)` |
| `POST /operations/parameter-sweep` | `run_parameter_sweep(...)` |
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from polymarket_trader.app.admin_query._helpers import (
    _compute_latency_payload,
    _empty_latency_payload,
)
from polymarket_trader.app.admin_serialization import AdminSerializer
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage

from ._db import RepositoryGroup, with_repositories
from .timeline_aggregator import TimelineAggregator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class AnalyticsAggregator:
    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession] | None",
        serializer: AdminSerializer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._serializer = serializer or AdminSerializer()
        self._timeline = TimelineAggregator(
            session_factory=session_factory, serializer=self._serializer
        )

    # ---------- edge realization ----------
    async def edge_realization_snapshot(
        self,
        *,
        limit: int = 200,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        from polymarket_trader.app.edge_realization import (
            aggregate_by_predicted_edge_buckets,
            build_edge_realization,
        )

        if self._session_factory is None:
            return {
                "items": [],
                "buckets": list(aggregate_by_predicted_edge_buckets(())),
                "limit": limit,
            }

        async def _query(repos: RepositoryGroup) -> tuple[Any, ...]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=limit,
                offset=0,
                accepted=True,
                condition_id=condition_id,
                time_range=time_range,
            )
            decisions = tuple(decision_page.items or ())
            keys = {(d.condition_id, d.token_id) for d in decisions if d.token_id}
            if not keys:
                return decisions, {}
            condition_ids = tuple({cid for cid, _ in keys})
            positions = await repos.position.list_by_condition_ids(condition_ids)
            position_records: dict[tuple[str, str], Any] = {
                (p.condition_id, p.token_id): p for p in positions
            }
            return decisions, position_records

        decisions, positions_by_key = await with_repositories(self._session_factory, _query)
        items = build_edge_realization(
            decisions=decisions,
            positions_by_key=positions_by_key,
        )
        return {
            "items": [item.as_payload() for item in items],
            "buckets": list(aggregate_by_predicted_edge_buckets(items)),
            "limit": limit,
        }

    # ---------- pnl breakdown ----------
    async def pnl_breakdown_snapshot(
        self,
        *,
        group_by: str,
        condition_id: str | None = None,
        position_limit: int = 5000,
    ) -> dict[str, Any]:
        from polymarket_trader.app.pnl_breakdown import (
            aggregate_totals,
            build_pnl_breakdown,
            is_valid_group_by,
            valid_group_by_values,
        )

        if not is_valid_group_by(group_by):
            raise ValueError(
                f"unsupported group_by: {group_by} (valid: {valid_group_by_values()})"
            )

        if self._session_factory is None:
            return {
                "group_by": group_by,
                "rows": [],
                "totals": aggregate_totals(()),
            }

        needs_markets = group_by in {"category", "outcome"}

        async def _query(repos: RepositoryGroup) -> tuple[Any, ...]:
            position_page = await repos.position.list_positions_snapshot(
                limit=position_limit,
                offset=0,
                condition_id=condition_id,
            )
            positions = tuple(position_page.items or ())
            if not needs_markets or not positions:
                return positions, {}
            condition_ids = tuple({p.condition_id for p in positions if p.condition_id})
            markets = await repos.market.list_by_condition_ids(condition_ids)
            markets_by_condition = {m.condition_id: m for m in markets}
            return positions, markets_by_condition

        positions, markets_by_condition = await with_repositories(self._session_factory, _query)
        rows = build_pnl_breakdown(
            positions=positions,
            markets_by_condition=markets_by_condition,
            group_by=group_by,
        )
        return {
            "group_by": group_by,
            "rows": [row.as_payload() for row in rows],
            "totals": aggregate_totals(rows),
        }

    # ---------- parameter sweep ----------
    async def run_parameter_sweep(
        self,
        *,
        candidates: dict[str, Any],
        per_decision_usdc: Decimal = Decimal("10"),
        time_range: TimeRange | None = None,
        decision_limit: int = 2000,
        settlement_limit: int = 2000,
    ) -> dict[str, Any]:
        from polymarket_trader.app.parameter_sweep import build_parameter_sweep

        if self._session_factory is None:
            return build_parameter_sweep(
                decisions=(),
                settlements=(),
                candidates=candidates,
                per_decision_usdc=per_decision_usdc,
            )

        async def _query(repos: RepositoryGroup) -> tuple[Any, Any]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=decision_limit,
                offset=0,
                time_range=time_range,
            )
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=settlement_limit,
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                time_range=None,
            )
            return decision_page, settle_page

        decision_page, settle_page = await with_repositories(self._session_factory, _query)
        return build_parameter_sweep(
            decisions=tuple(decision_page.items or ()),
            settlements=tuple(settle_page.items or ()),
            candidates=candidates,
            per_decision_usdc=per_decision_usdc,
        )

    # ---------- missed opportunities ----------
    async def missed_opportunities_snapshot(
        self,
        *,
        limit: int = 500,
        per_decision_usdc: Decimal = Decimal("10"),
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        from polymarket_trader.app.missed_opportunities import build_missed_opportunities

        if self._session_factory is None:
            return build_missed_opportunities(
                rejected_decisions=(),
                settlements=(),
                per_decision_usdc=per_decision_usdc,
            )

        async def _query(repos: RepositoryGroup) -> tuple[Any, Any]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=limit,
                offset=0,
                accepted=False,
                time_range=time_range,
            )
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=max(limit, 500),
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                time_range=None,
            )
            return decision_page, settle_page

        decision_page, settle_page = await with_repositories(self._session_factory, _query)
        return build_missed_opportunities(
            rejected_decisions=tuple(decision_page.items or ()),
            settlements=tuple(settle_page.items or ()),
            per_decision_usdc=per_decision_usdc,
        )

    # ---------- calibration ----------
    async def calibration_snapshot(
        self,
        *,
        bucket_size: Decimal = Decimal("0.05"),
        time_range: TimeRange | None = None,
        sample_limit: int = 2000,
    ) -> dict[str, Any]:
        from polymarket_trader.app.calibration import build_calibration

        if self._session_factory is None:
            return {
                "bucket_size": str(bucket_size),
                "buckets": [],
                "brier_score": None,
                "log_loss": None,
                "total_samples": 0,
                "with_outcome_count": 0,
            }

        async def _query(repos: RepositoryGroup) -> tuple[Any, Any]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=sample_limit,
                offset=0,
                accepted=True,
                time_range=time_range,
            )
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=sample_limit,
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                time_range=None,
            )
            return decision_page, settle_page

        decision_page, settle_page = await with_repositories(self._session_factory, _query)
        return build_calibration(
            decisions=tuple(decision_page.items or ()),
            settlements=tuple(settle_page.items or ()),
            bucket_size=bucket_size,
        )

    # ---------- latency percentiles ----------
    async def latency_percentiles_snapshot(
        self,
        *,
        window_ms: int | None = None,
        sample_limit: int = 500,
        event_types: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        types = event_types or (
            DomainEventType.ORDER_SUBMITTED.value,
            DomainEventType.ORDER_SIGNED.value,
            DomainEventType.ORDER_MATCHED.value,
            DomainEventType.ORDER_PARTIALLY_FILLED.value,
            DomainEventType.ORDER_NO_FILL.value,
            DomainEventType.ORDER_REJECTED.value,
            DomainEventType.ORDER_CANCEL_REQUESTED.value,
            DomainEventType.ORDER_CANCELLED.value,
            DomainEventType.REPLACE_ORDER_SUBMITTED.value,
        )

        time_range: TimeRange | None = None
        if window_ms is not None and window_ms > 0:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            time_range = TimeRange(since_ms=now_ms - window_ms, until_ms=now_ms)

        if self._session_factory is None:
            return _empty_latency_payload(types, sample_limit, window_ms)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.outbox.list_events_by_types_snapshot(
                event_types=types,
                limit=sample_limit,
                offset=0,
                time_range=time_range,
            )

        page = await with_repositories(self._session_factory, _query)
        events = tuple(page.items or ())
        return _compute_latency_payload(events, types, sample_limit, window_ms)

    # ---------- equity curve / risk metrics ----------
    async def portfolio_equity_curve(
        self,
        *,
        window_ms: int,
        interval_ms: int,
        account_key: str = "primary",
    ) -> dict[str, Any]:
        if self._session_factory is None:
            raise RuntimeError("db_session_factory unavailable")

        from polymarket_trader.app.portfolio_history_service import PortfolioHistoryService
        from polymarket_trader.infra.db import AccountSnapshotRepository

        session_factory = self._session_factory

        async def _query(*, since, until, interval_ms, account_key):
            async with session_factory() as session:
                repo = AccountSnapshotRepository(session)
                return await repo.query_history_bucketed(
                    since=since,
                    until=until,
                    interval_ms=interval_ms,
                    account_key=account_key,
                )

        service = PortfolioHistoryService(query_history=_query)
        result = await service.equity_curve(
            window_ms=window_ms,
            interval_ms=interval_ms,
            account_key=account_key,
        )
        return result.to_payload()

    async def portfolio_risk_metrics(
        self,
        *,
        window_ms: int,
        interval_ms: int,
        account_key: str = "primary",
        annualization_factor: float | None = None,
    ) -> dict[str, Any]:
        if self._session_factory is None:
            raise RuntimeError("db_session_factory unavailable")

        from polymarket_trader.app.portfolio_history_service import PortfolioHistoryService
        from polymarket_trader.app.risk_metrics import build_risk_metrics
        from polymarket_trader.infra.db import AccountSnapshotRepository

        session_factory = self._session_factory

        async def _query(*, since, until, interval_ms, account_key):
            async with session_factory() as session:
                repo = AccountSnapshotRepository(session)
                return await repo.query_history_bucketed(
                    since=since,
                    until=until,
                    interval_ms=interval_ms,
                    account_key=account_key,
                )

        service = PortfolioHistoryService(query_history=_query)
        result = await service.equity_curve(
            window_ms=window_ms,
            interval_ms=interval_ms,
            account_key=account_key,
        )
        return {
            "window_ms": result.window_ms,
            "interval_ms": result.interval_ms,
            "metrics": build_risk_metrics(
                result.points,
                annualization_factor=annualization_factor,
            ),
        }

    # ---------- risk rejections ----------
    async def list_risk_rejections(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        return await self._timeline.list_audit_events(
            limit=limit,
            offset=offset,
            event_title=DomainEventType.RISK_REJECTION_RECORDED.value,
            condition_id=condition_id,
            time_range=time_range,
        )

    async def aggregate_risk_rejections(
        self,
        *,
        time_range: TimeRange | None = None,
        condition_id: str | None = None,
        sample_limit: int = 1000,
    ) -> dict[str, Any]:
        if self._session_factory is None:
            return {"buckets": [], "total_rejections": 0}

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.audit.list_audit_events_snapshot(
                limit=sample_limit,
                offset=0,
                event_title=DomainEventType.RISK_REJECTION_RECORDED.value,
                condition_id=condition_id,
                time_range=time_range,
            )

        page = await with_repositories(self._session_factory, _query)

        check_counter: Counter[str] = Counter()
        field_counter: Counter[str] = Counter()
        total = 0
        for event in (page.items or ()):
            payload = event.payload if isinstance(event.payload, dict) else {}
            total += 1
            checks = payload.get("checks")
            if isinstance(checks, list):
                for check in checks:
                    if not isinstance(check, dict):
                        continue
                    if check.get("passed") is True:
                        continue
                    name = str(check.get("name") or "(unnamed)")
                    field = str(check.get("field") or "(none)")
                    check_counter[name] += 1
                    field_counter[field] += 1
        return {
            "total_rejections": total,
            "by_check_name": [
                {"check_name": name, "count": cnt}
                for name, cnt in check_counter.most_common(50)
            ],
            "by_failed_field": [
                {"field": fld, "count": cnt}
                for fld, cnt in field_counter.most_common(50)
            ],
        }
