"""分析类只读查询：edge 实现、PnL 分解、missed opportunities、校准、
latency 分位数、组合风险/equity 曲线、参数 sweep。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_trader.app.admin_query._helpers import (
    _compute_latency_payload,
    _empty_latency_payload,
)
from polymarket_trader.app.admin_service_helpers import _RepositoryGroup
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage


class AdminAnalyticsQueryMixin:
    """分析维度的只读查询——纯报表，不影响交易主链路。"""

    async def edge_realization_snapshot(
        self,
        *,
        limit: int = 200,
        strategy_id: str | None = None,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """Edge 实现度：预测 edge vs 实际 per-share 回报，按预测 edge 分桶聚合。

        只取 ``accepted=true`` 的决策（拒绝的没有 position 对应）；对每条决策
        从 ``decision_output`` 抽 ``fair_value`` 和 ``entry_price``，从 position 算
        ``realized_pnl / cost`` （已平仓优先）或 ``cash_pnl / cost`` （未平仓）。
        """

        from polymarket_trader.app.edge_realization import (
            aggregate_by_predicted_edge_buckets,
            build_edge_realization,
        )

        if not self._has_db_session_factory():
            return {
                "items": [],
                "buckets": list(aggregate_by_predicted_edge_buckets(())),
                "limit": limit,
            }

        async def _query(repos: _RepositoryGroup) -> tuple[Any, ...]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=limit,
                offset=0,
                accepted=True,
                condition_id=condition_id,
                strategy_id=strategy_id,
                time_range=time_range,
            )
            decisions = tuple(decision_page.items or ())
            keys = {(d.condition_id, d.token_id) for d in decisions if d.token_id}
            if not keys:
                return decisions, {}
            condition_ids = tuple({cid for cid, _ in keys})
            # 单次 IN 查询替代 N+1：edge-realization 的 condition_ids 可达数百，
            # 旧实现每个一次 await 会把响应放大百倍延迟。
            positions = await repos.position.list_by_condition_ids(
                condition_ids,
                strategy_id=strategy_id,
            )
            position_records: dict[tuple[str, str], Any] = {
                (p.condition_id, p.token_id): p for p in positions
            }
            return decisions, position_records

        decisions, positions_by_key = await self._with_repositories(_query)
        items = build_edge_realization(
            decisions=decisions,
            positions_by_key=positions_by_key,
        )
        return {
            "items": [item.as_payload() for item in items],
            "buckets": list(aggregate_by_predicted_edge_buckets(items)),
            "limit": limit,
        }

    async def pnl_breakdown_snapshot(
        self,
        *,
        group_by: str,
        strategy_id: str | None = None,
        condition_id: str | None = None,
        position_limit: int = 5000,
    ) -> dict[str, Any]:
        """按维度分解的仓位 PnL 聚合。

        合法 ``group_by`` 由 ``pnl_breakdown.valid_group_by_values()`` 暴露：
        ``strategy_id`` / ``market_slug`` / ``condition_id`` / ``category`` /
        ``outcome`` / ``redeemable_status``。``category`` 和 ``outcome`` 维度
        额外做一次 markets 批量 join。
        """

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

        if not self._has_db_session_factory():
            return {
                "group_by": group_by,
                "rows": [],
                "totals": aggregate_totals(()),
            }

        needs_markets = group_by in {"category", "outcome"}

        async def _query(repos: _RepositoryGroup) -> tuple[Any, ...]:
            position_page = await repos.position.list_positions_snapshot(
                limit=position_limit,
                offset=0,
                condition_id=condition_id,
                strategy_id=strategy_id,
            )
            positions = tuple(position_page.items or ())
            if not needs_markets or not positions:
                return positions, {}
            condition_ids = tuple({p.condition_id for p in positions if p.condition_id})
            markets = await repos.market.list_by_condition_ids(condition_ids)
            markets_by_condition = {m.condition_id: m for m in markets}
            return positions, markets_by_condition

        positions, markets_by_condition = await self._with_repositories(_query)
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

    async def run_parameter_sweep(
        self,
        *,
        candidates: dict[str, Any],
        per_decision_usdc: Decimal = Decimal("10"),
        strategy_id: str | None = None,
        time_range: TimeRange | None = None,
        decision_limit: int = 2000,
        settlement_limit: int = 2000,
    ) -> dict[str, Any]:
        """对历史决策回放给定参数候选笛卡尔积，输出每组 hypothetical PnL 排序。

        所有副作用都在内存（不改 ParameterStore、不下单），可在策略迭代期
        反复跑。``candidates`` 由调用层校验（白名单 + 笛卡尔积上限）。
        """

        from polymarket_trader.app.parameter_sweep import build_parameter_sweep
        from polymarket_trader.domain.events import DomainEventType

        if not self._has_db_session_factory():
            return build_parameter_sweep(
                decisions=(),
                settlements=(),
                candidates=candidates,
                per_decision_usdc=per_decision_usdc,
            )

        async def _query(repos: _RepositoryGroup) -> tuple[Any, Any]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=decision_limit,
                offset=0,
                strategy_id=strategy_id,
                time_range=time_range,
            )
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=settlement_limit,
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                time_range=None,
            )
            return decision_page, settle_page

        decision_page, settle_page = await self._with_repositories(_query)
        return build_parameter_sweep(
            decisions=tuple(decision_page.items or ()),
            settlements=tuple(settle_page.items or ()),
            candidates=candidates,
            per_decision_usdc=per_decision_usdc,
        )

    async def missed_opportunities_snapshot(
        self,
        *,
        limit: int = 500,
        per_decision_usdc: Decimal = Decimal("10"),
        strategy_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """对 ``accepted=false`` 的决策做事后盈利模拟。

        join settlement 事件 + 决策 token_id 判断"如果当时下单了赚还是亏"，
        按 reason 聚合——配合 ``risk_rejections`` 用，判断风控阈值是否过严。
        """

        from polymarket_trader.app.missed_opportunities import build_missed_opportunities
        from polymarket_trader.domain.events import DomainEventType

        if not self._has_db_session_factory():
            return build_missed_opportunities(
                rejected_decisions=(),
                settlements=(),
                per_decision_usdc=per_decision_usdc,
            )

        async def _query(repos: _RepositoryGroup) -> tuple[Any, Any]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=limit,
                offset=0,
                accepted=False,
                strategy_id=strategy_id,
                time_range=time_range,
            )
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=max(limit, 500),
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                time_range=None,
            )
            return decision_page, settle_page

        decision_page, settle_page = await self._with_repositories(_query)
        return build_missed_opportunities(
            rejected_decisions=tuple(decision_page.items or ()),
            settlements=tuple(settle_page.items or ()),
            per_decision_usdc=per_decision_usdc,
        )

    async def calibration_snapshot(
        self,
        *,
        bucket_size: Decimal = Decimal("0.05"),
        strategy_id: str | None = None,
        time_range: TimeRange | None = None,
        sample_limit: int = 2000,
    ) -> dict[str, Any]:
        """定价模型校准 + Brier score。

        对每个 ``accepted=true`` 的决策，按 ``decision_output.fair_value`` 分桶；
        每个桶在市场结算后统计实际命中率（市场 RESOLVED 且持仓 redeemable=true
        视为预测正确）。Brier 是 ``mean((fair_value - outcome)^2)``，越接近 0
        校准越好。
        """

        if not self._has_db_session_factory():
            return {
                "bucket_size": str(bucket_size),
                "buckets": [],
                "brier_score": None,
                "log_loss": None,
                "total_samples": 0,
                "with_outcome_count": 0,
            }

        from polymarket_trader.domain.events import DomainEventType

        async def _query(repos: _RepositoryGroup) -> tuple[Any, Any]:
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=sample_limit,
                offset=0,
                accepted=True,
                strategy_id=strategy_id,
                time_range=time_range,
            )
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=sample_limit,
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                time_range=None,
            )
            return decision_page, settle_page

        decision_page, settle_page = await self._with_repositories(_query)
        from polymarket_trader.app.calibration import build_calibration

        return build_calibration(
            decisions=tuple(decision_page.items or ()),
            settlements=tuple(settle_page.items or ()),
            bucket_size=bucket_size,
        )

    async def latency_percentiles_snapshot(
        self,
        *,
        window_ms: int | None = None,
        sample_limit: int = 500,
        event_types: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """计算订单执行 latency 分位数（queue→sign / sign→submit / submit→ack / queue→ack）。

        从 outbox_events.payload->'timestamps' 抽 ``queued_at`` /
        ``sign_started_at`` / ``signed_at`` / ``submitted_at`` / ``ack_at``，
        按事件 fan-out 计算 stage 间 latency 毫秒；P0 实时性观测刚需。
        """

        from polymarket_trader.domain.events import DomainEventType

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

        if not self._has_db_session_factory():
            return _empty_latency_payload(types, sample_limit, window_ms)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.outbox.list_events_by_types_snapshot(
                event_types=types,
                limit=sample_limit,
                offset=0,
                time_range=time_range,
            )

        page = await self._with_repositories(_query)
        events = tuple(page.items or ())
        return _compute_latency_payload(events, types, sample_limit, window_ms)

    async def portfolio_risk_metrics(
        self,
        *,
        window_ms: int,
        interval_ms: int,
        account_key: str = "primary",
        annualization_factor: float | None = None,
    ) -> dict[str, Any]:
        """组合级风险归因——max drawdown / time underwater / 波动率 / Sharpe-like。

        复用 ``equity-curve`` 同一份 downsampled 时间序列；空 / 单点序列也
        安全（相关指标返回 None 而不是抛错）。``annualization_factor`` 可
        选，如 365 表示按 1d 桶年化 Sharpe。
        """

        if not self._has_db_session_factory():
            raise RuntimeError("db_session_factory unavailable")

        session_factory = self.runtime.db_session_factory
        assert session_factory is not None  # narrowed by _has_db_session_factory above

        from polymarket_trader.app.portfolio_history_service import (
            PortfolioHistoryService,
        )
        from polymarket_trader.app.risk_metrics import build_risk_metrics
        from polymarket_trader.infra.db import AccountSnapshotRepository

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

    async def portfolio_equity_curve(
        self,
        *,
        window_ms: int,
        interval_ms: int,
        account_key: str = "primary",
    ) -> dict[str, Any]:
        """返回 ``GET /portfolio/equity-curve`` 投影。

        Downsampling 在 PG 内完成；服务层只对已聚合点做 drawdown 投影。
        没有 DB 时抛 ``RuntimeError``——这条接口本身就是审计用途，缺少持久化
        数据的语义不能用空数组掩盖。
        """

        if not self._has_db_session_factory():
            raise RuntimeError("db_session_factory unavailable")

        session_factory = self.runtime.db_session_factory
        assert session_factory is not None  # narrowed by _has_db_session_factory above

        from polymarket_trader.app.portfolio_history_service import (
            PortfolioHistoryService,
        )
        from polymarket_trader.infra.db import AccountSnapshotRepository

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


__all__ = ["AdminAnalyticsQueryMixin"]
