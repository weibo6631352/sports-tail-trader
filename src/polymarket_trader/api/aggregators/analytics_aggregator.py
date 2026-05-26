"""AnalyticsAggregator —— 报表类分析查询（DB-only，§12.2 审计查询类）。

按 原架构方案 §12.2。聚合所有基于 audit_events / decision_records /
positions 的 SQL 报表查询，不读运行时内存（无缓存，每次实时算）。

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

from polymarket_trader.api.serialization import ApiSerializer
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage

from ._db import RepositoryGroup, with_repositories
from ._helpers import compute_latency_payload as _compute_latency_payload
from ._helpers import empty_latency_payload as _empty_latency_payload
from .timeline_aggregator import TimelineAggregator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class AnalyticsAggregator:
    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession] | None",
        serializer: ApiSerializer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._serializer = serializer or ApiSerializer.from_runtime(None)
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
        from polymarket_trader.domain.analytics.edge_realization import (
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
        from polymarket_trader.domain.analytics.pnl_breakdown import (
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
        from polymarket_trader.domain.analytics.parameter_sweep import build_parameter_sweep

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
        from polymarket_trader.domain.analytics.missed_opportunities import build_missed_opportunities

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
        from polymarket_trader.domain.analytics.calibration import build_calibration

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

        from polymarket_trader.domain.analytics.portfolio_history_service import PortfolioHistoryService
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

        from polymarket_trader.domain.analytics.portfolio_history_service import PortfolioHistoryService
        from polymarket_trader.domain.analytics.risk_metrics import build_risk_metrics
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


    async def error_rate_timeseries(
        self: AnalyticsAggregator,
        *,
        window_minutes: int = 15,
    ) -> dict[str, object]:
        """错误率时序 — 最近 1/5/15min HTTP errors + audit errors + per-bucket。"""
        if self._session_factory is None:
            return {"buckets": [], "summary": {}}
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text as sql_text

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(
                    sql_text("""
                        SELECT
                          date_trunc('minute', created_at) as bucket,
                          event_title,
                          COUNT(*) FILTER (WHERE status IN ('rejected','failed','error')
                            OR reason LIKE '%error%' OR reason LIKE '%failed%') as errors,
                          COUNT(*) as total
                        FROM audit_events
                        WHERE created_at > :cutoff
                          AND event_title IN ('order_rejected','order_state_updated',
                            'order_submitted','risk_rejection_recorded','fill_recorded')
                        GROUP BY 1, 2
                        ORDER BY 1 DESC
                    """),
                    {"cutoff": cutoff},
                )).all()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        from collections import defaultdict as _defaultdict

        by_bucket: dict[str, dict] = _defaultdict(
            lambda: {"errors": 0, "total": 0, "by_event": {}}
        )
        for r in rows:
            b = r[0].isoformat()
            by_bucket[b]["errors"] += int(r[2] or 0)
            by_bucket[b]["total"] += int(r[3] or 0)
            by_bucket[b]["by_event"][r[1]] = {
                "errors": int(r[2] or 0), "total": int(r[3] or 0),
            }
        buckets = []
        for b in sorted(by_bucket.keys(), reverse=True):
            bk = by_bucket[b]
            buckets.append({
                "bucket": b,
                "errors": bk["errors"],
                "total": bk["total"],
                "error_rate_pct": (
                    round(bk["errors"] / bk["total"] * 100, 2) if bk["total"] else 0
                ),
                "by_event": bk["by_event"],
            })
        now = datetime.now(timezone.utc)
        summary: dict[str, dict] = {}
        for w_min in (1, 5, 15):
            w_cutoff = now - timedelta(minutes=w_min)
            w_errors = sum(
                b["errors"] for b in buckets
                if datetime.fromisoformat(b["bucket"]) > w_cutoff
            )
            w_total = sum(
                b["total"] for b in buckets
                if datetime.fromisoformat(b["bucket"]) > w_cutoff
            )
            summary[f"last_{w_min}min"] = {
                "errors": w_errors,
                "total": w_total,
                "error_rate_pct": (
                    round(w_errors / w_total * 100, 2) if w_total else 0
                ),
            }
        return {
            "window_minutes": window_minutes,
            "buckets_count": len(buckets),
            "summary": summary,
            "buckets": buckets[:30],
        }

    async def guard_stats_snapshot(
        self: AnalyticsAggregator,
        *,
        window_minutes: int = 60,
    ) -> dict[str, object]:
        """守卫触发统计：每个 reject reason 在过去 N 分钟触发的次数。"""
        if self._session_factory is None:
            return {"items": [], "window_minutes": window_minutes}
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text as sql_text

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sql_text("""
                        SELECT
                          COALESCE(payload->>'reason', reason, '?') as r,
                          event_title,
                          COUNT(*) as n
                        FROM audit_events
                        WHERE created_at > :cutoff
                          AND event_title IN ('order_rejected','risk_rejection_recorded',
                                              'allocation_decision_recorded','market_filtered_out')
                        GROUP BY 1, 2
                        ORDER BY n DESC
                        LIMIT 50
                    """),
                    {"cutoff": cutoff},
                )
                rows = result.all()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "items": [], "window_minutes": window_minutes}
        items = [{"reason": r[0], "event": r[1], "count": r[2]} for r in rows]
        return {
            "window_minutes": window_minutes,
            "total": sum(i["count"] for i in items),
            "items": items,
        }

    async def win_rate_breakdown(
        self: AnalyticsAggregator,
        *,
        window_hours: int = 168,
    ) -> dict[str, object]:
        """历史 BUY+SELL 配对胜率分组（per price bucket / sport / market_type）。"""
        if self._session_factory is None:
            return {"groups": [], "window_hours": window_hours}
        from collections import defaultdict as _defaultdict
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal as _D

        from sqlalchemy import text as sql_text

        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sql_text("""
                        SELECT
                          a.payload->'fill'->>'side' as side,
                          a.payload->'fill'->>'token_id' as token_id,
                          (a.payload->'fill'->>'price')::numeric as price,
                          (a.payload->'fill'->>'size')::numeric as size,
                          (a.payload->'fill'->>'notional_usdc')::numeric as notional,
                          m.market_slug,
                          a.created_at
                        FROM audit_events a
                        LEFT JOIN markets m
                          ON m.condition_id = a.payload->'fill'->>'condition_id'
                        WHERE a.event_title='fill_recorded'
                          AND a.created_at > :cutoff
                          AND a.payload->'fill' IS NOT NULL
                        ORDER BY a.created_at
                    """),
                    {"cutoff": cutoff},
                )
                fills = list(result)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "groups": [], "window_hours": window_hours}

        positions: dict[str, list] = _defaultdict(list)
        closed_trades: list[dict] = []
        token_to_slug: dict[str, str] = {}
        for row in fills:
            side = (row[0] or "").lower()
            token = row[1] or ""
            price = _D(str(row[2])) if row[2] is not None else _D("0")
            size = _D(str(row[3])) if row[3] is not None else _D("0")
            notional = _D(str(row[4])) if row[4] is not None else _D("0")
            slug = row[5] or ""
            if slug:
                token_to_slug[token] = slug
            if size <= 0:
                continue
            if side == "buy":
                positions[token].append({"cost": notional, "shares": size, "price": price})
            elif side == "sell":
                remaining = size
                while remaining > 0 and positions[token]:
                    buy = positions[token][0]
                    use_shares = min(buy["shares"], remaining)
                    cost_ratio = use_shares / buy["shares"] if buy["shares"] > 0 else _D("0")
                    matched_cost = buy["cost"] * cost_ratio
                    matched_revenue = price * use_shares
                    pnl = matched_revenue - matched_cost
                    closed_trades.append({
                        "token": token,
                        "market_slug": token_to_slug.get(token, ""),
                        "buy_price": float(buy["price"]),
                        "sell_price": float(price),
                        "cost": float(matched_cost),
                        "revenue": float(matched_revenue),
                        "pnl": float(pnl),
                    })
                    buy["shares"] -= use_shares
                    buy["cost"] -= matched_cost
                    if buy["shares"] <= _D("0.0000001"):
                        positions[token].pop(0)
                    remaining -= use_shares

        def _price_bucket(p: float) -> str:
            if p < 0.2:
                return "0.0-0.2"
            if p < 0.4:
                return "0.2-0.4"
            if p < 0.6:
                return "0.4-0.6"
            if p < 0.8:
                return "0.6-0.8"
            return "0.8-1.0"

        def _sport_from_slug(slug: str) -> str:
            sl = slug.lower()
            for s in (
                "kbo", "mlb", "nba", "wnba", "nhl", "nfl", "ncaaf", "ncaab",
                "atp", "wta", "itf", "mls", "epl", "laliga", "j2100", "j1100",
                "j3100", "chi", "bra", "arg", "mex",
            ):
                if s in sl:
                    return s
            return "other"

        def _market_type_from_slug(slug: str) -> str:
            sl = slug.lower()
            if "spread" in sl:
                return "spread"
            if "total" in sl:
                return "total"
            if "halftime" in sl:
                return "halftime"
            if "first-set" in sl or "set-winner" in sl:
                return "set_prop"
            if "exact-score" in sl or "correct-score" in sl:
                return "exact_score"
            if "nrfi" in sl:
                return "nrfi"
            if "winner" in sl or sl.count("-") <= 3:
                return "moneyline"
            return "prop"

        def _aggregate(pnls: list[float], dim: str, value: str) -> dict:
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            total_pnl = sum(pnls)
            total_loss = sum(losses)
            return {
                "dimension": dim,
                "value": value,
                "trade_count": len(pnls),
                "win_count": len(wins),
                "winrate": round(len(wins) / len(pnls), 3) if pnls else 0,
                "avg_pnl": round(total_pnl / len(pnls), 4) if pnls else 0,
                "total_pnl": round(total_pnl, 4),
                "avg_win": round(sum(wins) / len(wins), 4) if wins else 0,
                "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0,
                "profit_factor": (
                    round(sum(wins) / abs(total_loss), 3) if total_loss < 0 else None
                ),
            }

        by_bucket: dict[str, list[float]] = _defaultdict(list)
        by_sport: dict[str, list[float]] = _defaultdict(list)
        by_type: dict[str, list[float]] = _defaultdict(list)
        by_sport_bucket: dict[tuple[str, str], list[float]] = _defaultdict(list)
        for trade in closed_trades:
            bucket = _price_bucket(trade["buy_price"])
            sport = _sport_from_slug(trade["market_slug"])
            mtype = _market_type_from_slug(trade["market_slug"])
            by_bucket[bucket].append(trade["pnl"])
            by_sport[sport].append(trade["pnl"])
            by_type[mtype].append(trade["pnl"])
            by_sport_bucket[(sport, bucket)].append(trade["pnl"])

        groups_bucket = [
            _aggregate(p, "buy_price_bucket", b) for b, p in sorted(by_bucket.items())
        ]
        groups_sport = sorted(
            (_aggregate(p, "sport", s) for s, p in by_sport.items()),
            key=lambda g: g["total_pnl"], reverse=True,
        )
        groups_type = sorted(
            (_aggregate(p, "market_type", t) for t, p in by_type.items()),
            key=lambda g: g["total_pnl"], reverse=True,
        )
        groups_sport_bucket = sorted(
            (
                _aggregate(p, "sport_x_price", f"{s}/{b}")
                for (s, b), p in by_sport_bucket.items()
            ),
            key=lambda g: g["total_pnl"], reverse=True,
        )

        return {
            "window_hours": window_hours,
            "total_fills": len(fills),
            "closed_trades": len(closed_trades),
            "open_positions_count": sum(1 for _tok, q in positions.items() if q),
            "total_realized_pnl": round(sum(t["pnl"] for t in closed_trades), 4),
            "by_price_bucket": groups_bucket,
            "by_sport": groups_sport,
            "by_market_type": groups_type,
            "by_sport_x_price": groups_sport_bucket[:20],
            "recent_trades_sample": closed_trades[-10:],
        }

    async def funnel(
        self: AnalyticsAggregator,
        *,
        window_ms: int,
        end_ms: int | None = None,
        league: str | None = None,
        market_type: str | None = None,
    ) -> dict[str, Any]:
        """入场漏斗各阶段计数（discovered → eligible → candidate → accepted → ...）。"""
        from polymarket_trader.infra.db.analytics_queries import (
            FUNNEL_STAGES,
            fetch_funnel_counts,
        )

        if self._session_factory is None:
            return {
                "window_ms": window_ms,
                "stages": [{"name": name, "count": 0} for name in FUNNEL_STAGES],
                "filters": {"league": league, "market_type": market_type},
            }
        start_dt, end_dt = _resolve_window(window_ms=window_ms, end_ms=end_ms)
        async with self._session_factory() as session:
            counts = await fetch_funnel_counts(
                session,
                window_start=start_dt, window_end=end_dt,
                league=league, market_type=market_type,
            )
        stages = [
            {"name": name, "count": int(counts.get(name, 0))} for name in FUNNEL_STAGES
        ]
        return {
            "window_ms": window_ms,
            "generated_at": end_dt.isoformat(),
            "stages": stages,
            "filters": {"league": league, "market_type": market_type},
        }

    async def rejections(
        self: AnalyticsAggregator,
        *,
        window_ms: int,
        end_ms: int | None = None,
        league: str | None = None,
        market_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """拒绝原因 Top N（按 reason 聚合，按出现次数降序）。"""
        from polymarket_trader.infra.db.analytics_queries import fetch_rejection_reasons

        if self._session_factory is None:
            return {
                "window_ms": window_ms,
                "total": 0,
                "top": [],
                "filters": {"league": league, "market_type": market_type},
            }
        start_dt, end_dt = _resolve_window(window_ms=window_ms, end_ms=end_ms)
        async with self._session_factory() as session:
            total, rows = await fetch_rejection_reasons(
                session,
                window_start=start_dt, window_end=end_dt,
                league=league, market_type=market_type, limit=limit,
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
        self: AnalyticsAggregator,
        *,
        window_ms: int,
        end_ms: int | None = None,
        league: str | None = None,
        market_type: str | None = None,
    ) -> dict[str, Any]:
        """订单执行质量（submit / fill latency 分位 + slippage bps）。"""
        from polymarket_trader.infra.db.analytics_queries import fetch_execution_quality

        if self._session_factory is None:
            return {
                "window_ms": window_ms,
                "submit_latency_ms": {"p50": None, "p95": None},
                "fill_latency_ms": {"p50": None, "p95": None},
                "slippage_bps": {"mean": None, "p95": None},
                "sample_size": 0,
                "filters": {"league": league, "market_type": market_type},
            }
        start_dt, end_dt = _resolve_window(window_ms=window_ms, end_ms=end_ms)
        async with self._session_factory() as session:
            data = await fetch_execution_quality(
                session,
                window_start=start_dt, window_end=end_dt,
                league=league, market_type=market_type,
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
# ===== 扩展方法：错误率时序 / 守卫统计 / 历史胜率分组 =====
# 这 3 个方法都基于 audit_events SQL 聚合，属于审计查询类（§12.2）。
# 用 monkey-patch 挂到 AnalyticsAggregator 上，与上面 12 个类方法等价；
# 后续 W6 改造时统一收编到 class body。


# ===== 漏斗 / 拒绝原因 top / 执行质量（W5 收口：原 AnalyticsService 三方法） =====
# 这 3 个查询的窗口语义（window_ms + end_ms → start_dt/end_dt）与 AnalyticsAggregator
# 其他方法（time_range 风格）不一致，保留入参形态以维持前端契约 byte-for-byte 一致。


def _resolve_window(
    *, window_ms: int, end_ms: int | None
) -> tuple[datetime, datetime]:
    from datetime import datetime as _dt, timezone as _tz
    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    end_dt = (
        _dt.fromtimestamp(end_ms / 1000.0, tz=_tz.utc)
        if end_ms is not None else _dt.now(_tz.utc)
    )
    start_dt = _dt.fromtimestamp(
        (int(end_dt.timestamp() * 1000) - window_ms) / 1000.0, tz=_tz.utc,
    )
    return start_dt, end_dt


def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None

