"""Analytics 只读接口（漏斗 / 拒绝原因 / 执行质量 + edge / 校准 / missed 等报表）。

不在路由层执行业务规则；统一走 AnalyticsAggregator（§12.2 审计查询类）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.aggregators import AnalyticsAggregator
from polymarket_trader.api.deps import build_time_range, get_runtime

# 默认窗口：24h；本地常量，不进 Settings。
DEFAULT_WINDOW_MS: int = 86_400_000

# 上限保护：最长 7 天，避免误传超大窗口拖垮 DB。
MAX_WINDOW_MS: int = 7 * 24 * 60 * 60 * 1000

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/funnel")
async def get_funnel(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, gt=0, le=MAX_WINDOW_MS),
    end_ms: int | None = Query(default=None, ge=0),
    league: str | None = Query(default=None, min_length=1, max_length=64),
    market_type: str | None = Query(default=None, min_length=1, max_length=64),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    return await AnalyticsAggregator(
        session_factory=runtime.db_session_factory,
    ).funnel(
        window_ms=window_ms,
        end_ms=end_ms,
        league=league,
        market_type=market_type,
    )


@router.get("/rejections")
async def get_rejections(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, gt=0, le=MAX_WINDOW_MS),
    end_ms: int | None = Query(default=None, ge=0),
    league: str | None = Query(default=None, min_length=1, max_length=64),
    market_type: str | None = Query(default=None, min_length=1, max_length=64),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    return await AnalyticsAggregator(
        session_factory=runtime.db_session_factory,
    ).rejections(
        window_ms=window_ms,
        end_ms=end_ms,
        league=league,
        market_type=market_type,
        limit=20,
    )


@router.get("/execution-quality")
async def get_execution_quality(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, gt=0, le=MAX_WINDOW_MS),
    end_ms: int | None = Query(default=None, ge=0),
    league: str | None = Query(default=None, min_length=1, max_length=64),
    market_type: str | None = Query(default=None, min_length=1, max_length=64),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    return await AnalyticsAggregator(
        session_factory=runtime.db_session_factory,
    ).execution_quality(
        window_ms=window_ms,
        end_ms=end_ms,
        league=league,
        market_type=market_type,
    )


@router.get("/edge-realization")
async def get_edge_realization(
    limit: int = Query(default=200, ge=1, le=2000),
    condition_id: str | None = Query(default=None, min_length=1),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """Edge 实现度：预测 edge vs 实际 per-share 回报。"""
    return await AnalyticsAggregator(
        session_factory=runtime.db_session_factory,
    ).edge_realization_snapshot(
        limit=limit,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/risk-rejections")
async def list_risk_rejections(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """风控结构化拒绝详情。"""
    return await AnalyticsAggregator(
        session_factory=runtime.db_session_factory,
    ).list_risk_rejections(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/risk-rejections/aggregate")
async def aggregate_risk_rejections(
    sample_limit: int = Query(default=1000, ge=1, le=5000),
    condition_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """按 check_name 和 failed_field 聚合风控拒绝。"""
    return await AnalyticsAggregator(
        session_factory=runtime.db_session_factory,
    ).aggregate_risk_rejections(
        sample_limit=sample_limit,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/calibration")
async def get_calibration(
    bucket_size: float = Query(default=0.05, gt=0.0, le=0.5),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    sample_limit: int = Query(default=2000, ge=1, le=10000),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """定价模型校准 + Brier score / log-loss。"""
    from decimal import Decimal

    return await AnalyticsAggregator(
        session_factory=runtime.db_session_factory,
    ).calibration_snapshot(
        bucket_size=Decimal(str(bucket_size)),
        time_range=build_time_range(since=since, until=until),
        sample_limit=sample_limit,
    )


@router.get("/missed-opportunities")
async def get_missed_opportunities(
    limit: int = Query(default=500, ge=1, le=5000),
    per_decision_usdc: float = Query(default=10.0, gt=0, le=10_000),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """被风控/决策器拒绝的决策事后盈利模拟。"""
    from decimal import Decimal

    return await AnalyticsAggregator(
        session_factory=runtime.db_session_factory,
    ).missed_opportunities_snapshot(
        limit=limit,
        per_decision_usdc=Decimal(str(per_decision_usdc)),
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/quant-summary")
async def get_quant_summary(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, gt=0, le=MAX_WINDOW_MS),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """量化决策器一站汇总——operator 看一眼就知道全局表现.

    聚合维度:
    - decision_counts: 决策总量 + accepted/rejected 占比
    - rejection_top: rejection reason top 5
    - kelly_stats: Kelly 内核统计 (avg prob_p / edge_net / f_star / budget,
      capped_by 分布)
    - portfolio: balance / net_value / cash_pnl / position_count / drawdown
    - market_coverage: tracked / live_state / signal_allowed 各 N 个
    - signal_health: 数据源新鲜度 / odds 覆盖率
    - execution: submit/fill latency p50/p95 + slippage

    全部内存或单 DB 聚合 query, < 100ms.
    """
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from sqlalchemy import text

    summary: dict[str, Any] = {
        "window_ms": window_ms,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(milliseconds=window_ms)

    # 1) decision_counts + rejection top + kelly_stats from decision_records
    session_factory = runtime.db_session_factory
    if session_factory is not None:
        async with session_factory() as session:
            counts_sql = text("""
                SELECT
                  count(*) AS total,
                  sum(CASE WHEN accepted THEN 1 ELSE 0 END) AS accepted,
                  sum(CASE WHEN NOT accepted THEN 1 ELSE 0 END) AS rejected
                FROM decision_records
                WHERE created_at >= :start AND created_at < :end
            """)
            row = (await session.execute(counts_sql, {"start": window_start, "end": window_end})).one()
            total = int(row[0] or 0)
            accepted = int(row[1] or 0)
            rejected = int(row[2] or 0)
            summary["decision_counts"] = {
                "total": total,
                "accepted": accepted,
                "rejected": rejected,
                "accept_rate_pct": round(accepted / total * 100, 2) if total else 0.0,
            }

            rej_sql = text("""
                SELECT reason, count(*) AS n
                FROM decision_records
                WHERE created_at >= :start AND created_at < :end AND NOT accepted
                GROUP BY reason ORDER BY n DESC LIMIT 5
            """)
            rej_rows = (await session.execute(rej_sql, {"start": window_start, "end": window_end})).all()
            summary["rejection_top"] = [
                {"reason": str(r[0]), "count": int(r[1]), "pct": round(int(r[1]) / rejected * 100, 2) if rejected else 0.0}
                for r in rej_rows
            ]

            kelly_sql = text("""
                SELECT
                  count(*) AS n,
                  avg((decision_output->'metadata'->'kelly'->>'prob_p')::numeric) AS avg_prob_p,
                  avg((decision_output->'metadata'->'kelly'->>'price_c')::numeric) AS avg_price_c,
                  avg((decision_output->'metadata'->'kelly'->>'edge_net')::numeric) AS avg_edge_net,
                  avg((decision_output->'metadata'->'kelly'->>'f_star')::numeric) AS avg_f_star,
                  avg((decision_output->'metadata'->'kelly'->>'buy_budget_usdc')::numeric) AS avg_budget,
                  sum(CASE WHEN (decision_output->'metadata'->'kelly'->>'is_round_up_overbet')='true' THEN 1 ELSE 0 END) AS rounded_up
                FROM decision_records
                WHERE created_at >= :start AND created_at < :end
                  AND accepted AND decision_output->'metadata'->'kelly' IS NOT NULL
            """)
            k_row = (await session.execute(kelly_sql, {"start": window_start, "end": window_end})).one()
            k_n = int(k_row[0] or 0)
            summary["kelly_stats"] = {
                "sample_count": k_n,
                "avg_prob_p": float(k_row[1]) if k_row[1] is not None else None,
                "avg_price_c": float(k_row[2]) if k_row[2] is not None else None,
                "avg_edge_net": float(k_row[3]) if k_row[3] is not None else None,
                "avg_f_star": float(k_row[4]) if k_row[4] is not None else None,
                "avg_buy_budget_usdc": float(k_row[5]) if k_row[5] is not None else None,
                "rounded_up_count": int(k_row[6] or 0),
                "rounded_up_pct": round(int(k_row[6] or 0) / k_n * 100, 2) if k_n else 0.0,
            }

            # 5) execution: order_submit + fill latency from audit
            exec_sql = text("""
                SELECT
                  count(*) FILTER (WHERE event_title='order_submitted') AS submitted,
                  count(*) FILTER (WHERE event_title='order_matched' AND status IN ('full_fill','partial_fill')) AS filled,
                  count(*) FILTER (WHERE event_title='order_rejected') AS order_rejected
                FROM audit_events
                WHERE created_at >= :start AND created_at < :end
            """)
            e_row = (await session.execute(exec_sql, {"start": window_start, "end": window_end})).one()
            summary["execution"] = {
                "orders_submitted": int(e_row[0] or 0),
                "orders_filled": int(e_row[1] or 0),
                "orders_rejected": int(e_row[2] or 0),
                "fill_rate_pct": round(int(e_row[1] or 0) / int(e_row[0] or 1) * 100, 2) if e_row[0] else 0.0,
            }
    else:
        summary["decision_counts"] = {"total": 0, "accepted": 0, "rejected": 0, "accept_rate_pct": 0.0}
        summary["rejection_top"] = []
        summary["kelly_stats"] = {"sample_count": 0}
        summary["execution"] = {"orders_submitted": 0, "orders_filled": 0, "orders_rejected": 0}

    # 2) portfolio (memory)
    try:
        account = runtime.account_state_store.snapshot()
        net_value = account.balance_usdc + sum(
            (p.shares * (p.cur_price or Decimal("0")) for p in account.positions),
            start=Decimal("0"),
        )
        cost_total = sum((p.cost_usdc for p in account.positions), start=Decimal("0"))
        summary["portfolio"] = {
            "balance_usdc": str(account.balance_usdc),
            "net_value_usdc": str(net_value),
            "cost_usdc": str(cost_total),
            "cash_pnl_usdc": str(net_value - account.balance_usdc - cost_total + cost_total),
            "position_count": len(account.positions),
            "open_order_count": len(account.open_orders),
            "paused_market_count": len(account.market_pauses),
        }
    except Exception as exc:  # noqa: BLE001
        summary["portfolio"] = {"error": str(exc)[:120]}

    # 3) market_coverage (memory)
    try:
        registry = runtime.registry.snapshot()
        metadata = runtime.market_metadata_store
        live_state_count = sum(1 for r in metadata.records() if r.live_state_payload)
        signal_allowed = sum(1 for r in metadata.records() if r.live_state_signal_allowed)
        summary["market_coverage"] = {
            "registry_total": len(registry.markets),
            "with_live_state": live_state_count,
            "signal_allowed": signal_allowed,
        }
    except Exception as exc:  # noqa: BLE001
        summary["market_coverage"] = {"error": str(exc)[:120]}

    # 4) signal_health
    try:
        from polymarket_trader.runtime.ws_loops import market_ws_subscription_token_ids
        summary["signal_health"] = {
            "ws_subscribed_tokens": len(market_ws_subscription_token_ids(runtime)),
            "user_ws_connected": account.user_ws_connected,
            "last_reconcile_at": (
                account.last_reconcile_at.isoformat() if account.last_reconcile_at else None
            ),
        }
    except Exception as exc:  # noqa: BLE001
        summary["signal_health"] = {"error": str(exc)[:120]}

    return summary


@router.get("/edge-signals")
async def get_edge_signals(
    k: int = Query(default=10, gt=0, le=100, description="Top-K edge candidates"),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """暴露 LiveSignalSnapshot ring buffer 的 top-K 差价候选。

    edge_pp = goalserve_fair_prob - pm_best_ask（正值 = 市场低估赢方 = 入场机会）。
    数据源：``runtime.signal_snapshot_store`` 内存 ring buffer，每 token 最近 300
    条 snapshot，约 5 分钟历史窗口。**无 DB 查询**，纯内存读，< 10ms。

    返回字段：
    - ``store_token_count``：当前 store 内有过 snapshot 记录的 token 数
    - ``top_k``：按 edge_pp desc 排序的 top-K 候选，每条含完整 snapshot 字段

    R6 注意：当前**无 caller 主动写入** snapshot——estimate_signal 返三元组但调用方
    选择性记录（CPO Round 2 R6 范围）。endpoint 框架先就位，等 R7+ 接入 caller 后
    才有真数据。空返回不算 endpoint bug。
    """

    store = runtime.signal_snapshot_store
    top = store.top_by_edge(k=k)
    return {
        "store_token_count": store.token_count(),
        "k": k,
        "top_k": [
            {
                "condition_id": snap.condition_id,
                "token_id": snap.token_id,
                "pm_best_ask": str(snap.pm_best_ask) if snap.pm_best_ask is not None else None,
                "pm_best_bid": str(snap.pm_best_bid) if snap.pm_best_bid is not None else None,
                "goalserve_fair_prob": (
                    str(snap.goalserve_fair_prob) if snap.goalserve_fair_prob is not None else None
                ),
                "math_prob": str(snap.math_prob) if snap.math_prob is not None else None,
                "microprice": str(snap.microprice) if snap.microprice is not None else None,
                "final_prob_p": str(snap.final_prob_p),
                "source_used": snap.source_used,
                "edge_pp": str(snap.edge_pp) if snap.edge_pp is not None else None,
                "timestamp": snap.timestamp.isoformat(),
            }
            for snap in top
        ],
    }


@router.get("/edge-signals/{token_id}")
async def get_edge_signal_history(
    token_id: str,
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """取某 token 的完整 snapshot 历史（按写入顺序，最多 300 条 ≈ 5 分钟）。

    用于 R7 前端 spark / 单条复盘 drawer——operator 可以看到某 condition 的
    edge 随时间演化（goalserve_fair_prob 在 进球/红牌 后突变？是否被 microprice
    污染兜底？）。
    """

    store = runtime.signal_snapshot_store
    history = store.history_for_token(token_id)
    return {
        "token_id": token_id,
        "count": len(history),
        "snapshots": [
            {
                "pm_best_ask": str(s.pm_best_ask) if s.pm_best_ask is not None else None,
                "pm_best_bid": str(s.pm_best_bid) if s.pm_best_bid is not None else None,
                "goalserve_fair_prob": (
                    str(s.goalserve_fair_prob) if s.goalserve_fair_prob is not None else None
                ),
                "math_prob": str(s.math_prob) if s.math_prob is not None else None,
                "microprice": str(s.microprice) if s.microprice is not None else None,
                "final_prob_p": str(s.final_prob_p),
                "source_used": s.source_used,
                "edge_pp": str(s.edge_pp) if s.edge_pp is not None else None,
                "timestamp": s.timestamp.isoformat(),
            }
            for s in history
        ],
    }


__all__ = ("router", "DEFAULT_WINDOW_MS", "MAX_WINDOW_MS")
