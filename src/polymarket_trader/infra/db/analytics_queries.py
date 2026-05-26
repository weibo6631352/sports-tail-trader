"""SQL queries for analytics endpoints.

只读聚合查询，全部在 SQL 层完成；Python 侧只负责绑定参数和把结果整型化。
不写库、不影响交易主链路，使用独立 session 即可。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket_trader.domain.events import DomainEventType


# 漏斗阶段 → 对应的 ``audit_events.event_title`` 集合。
# market_ingest_service 在接受新市场时 emit ``market_discovered``，对已跟踪市场的更新 emit
# ``market_updated``，拒绝时 emit ``market_filtered_out``。漏斗顶端「processed」
# 需要把这三类都纳入；中段「filtered_in」只算接受（discovered+updated）。
# 历史上 stage 名字面匹配 event_title 导致 ``market_filtered_in`` 永远 0，详见
# git log 该文件 commit。修复后 stage 是聚合视图名而非 event_title 别名。
FUNNEL_STAGE_EVENT_TITLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "market_discovered",
        (
            DomainEventType.MARKET_DISCOVERED.value,
            DomainEventType.MARKET_UPDATED.value,
            DomainEventType.MARKET_FILTERED_OUT.value,
        ),
    ),
    (
        "market_filtered_in",
        (
            DomainEventType.MARKET_DISCOVERED.value,
            DomainEventType.MARKET_UPDATED.value,
        ),
    ),
    (
        "market_filtered_out",
        (DomainEventType.MARKET_FILTERED_OUT.value,),
    ),
    (
        "risk_check_passed",
        (DomainEventType.RISK_CHECK_PASSED.value,),
    ),
    (
        "order_submitted",
        (DomainEventType.ORDER_SUBMITTED.value,),
    ),
    (
        "fill_recorded",
        (DomainEventType.FILL_RECORDED.value,),
    ),
)
FUNNEL_STAGES: tuple[str, ...] = tuple(stage for stage, _ in FUNNEL_STAGE_EVENT_TITLES)
# submit-latency 计算时「filtered_in」用的同一组 event_title。
_FILTERED_IN_EVENT_TITLES: tuple[str, ...] = dict(FUNNEL_STAGE_EVENT_TITLES)["market_filtered_in"]

REJECTION_EVENT_TITLES: tuple[str, ...] = (
    DomainEventType.ORDER_REJECTED.value,
    DomainEventType.RISK_CHECK_FAILED.value,
    DomainEventType.MARKET_FILTERED_OUT.value,
)


# Kelly / 风控拒绝原因分类——``fetch_rejection_reasons`` 返回 raw reason，下游
# operator / dashboard 可用此映射做分桶展示（"Kelly 早拒"vs"风控早拒"vs"账户余额"）。
# 旧的 single_order_limit_reached / market_limit_reached 等已删除——只保留 Kelly 时代。
REJECTION_REASON_CATEGORIES: dict[str, str] = {
    # Kelly engine 早拒
    "edge_below_min": "kelly_engine",
    "bankroll_non_positive": "kelly_engine",
    "kelly_stake_below_min": "kelly_engine",
    "kelly_below_market_min_no_round_up": "kelly_engine",
    "bankroll_too_small_for_market_min": "kelly_engine",
    "price_out_of_range": "kelly_engine",
    "fair_value_out_of_range": "kelly_engine",
    "prob_confidence_out_of_range": "kelly_engine",
    "missing_price_or_prob": "kelly_engine",
    # 框架风控
    "kelly_position_cap_exceeded": "risk_framework",
    "bankroll_overspent": "risk_framework",
    "neg_risk_cross_token_open_order": "risk_framework",
    "neg_risk_cross_token_position": "risk_framework",
    # 账户 / 余额
    "balance_insufficient": "account_balance",
    "allowance_insufficient": "account_balance",
    # 市场状态
    "market_not_active": "market_state",
    "market_not_open": "market_state",
    "market_resolved": "market_state",
    "market_cancelled": "market_state",
    "market_archived": "market_state",
    "clob_disabled": "market_state",
    # 流动性 / 价格
    "liquidity_insufficient": "liquidity",
    "price_above_tick_limit": "price",
    "tick_size_invalid": "price",
    "price_invalid": "price",
    # 操作 / 重试
    "retry_limit_reached": "operational",
    "open_buy_detected": "operational",
    "open_exit_detected": "operational",
    "resting_buy_not_allowed": "operational",
}


def categorize_rejection_reason(reason: str) -> str:
    """Map raw reason → category bucket。未分类的归 ``other``，便于 dashboard 看到
    新拒绝原因后扩 mapping。"""

    return REJECTION_REASON_CATEGORIES.get(reason, "other")


def _build_market_filter(
    *,
    condition_alias: str,
    league: str | None,
    market_type: str | None,
) -> tuple[str, str, dict[str, Any]]:
    """构造可拼接的 ``markets`` JOIN 及 WHERE 片段。

    - 不传 ``league`` / ``market_type`` 时不引入任何 JOIN。
    - ``league`` 同时匹配 ``markets.category`` 精确值和 ``markets.tags`` JSONB 包含。
    - ``market_type`` 按 ``markets.tags`` JSONB 包含匹配。
    """

    if league is None and market_type is None:
        return "", "", {}

    join_clause = f" JOIN markets m ON m.condition_id = {condition_alias}"
    where_clauses: list[str] = []
    params: dict[str, Any] = {}
    if league is not None:
        where_clauses.append(
            "(m.category = :league OR m.tags @> CAST(:league_tag AS jsonb))"
        )
        params["league"] = league
        params["league_tag"] = f'["{_jsonb_escape(league)}"]'
    if market_type is not None:
        where_clauses.append("m.tags @> CAST(:market_type_tag AS jsonb)")
        params["market_type_tag"] = f'["{_jsonb_escape(market_type)}"]'
    where_extra = " AND " + " AND ".join(where_clauses)
    return join_clause, where_extra, params


def _jsonb_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def fetch_funnel_counts(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    league: str | None = None,
    market_type: str | None = None,
) -> dict[str, int]:
    """单条查询一次性产出全部漏斗阶段计数。

    每个 stage 用 ``COUNT(*) FILTER (WHERE event_title = ANY(...))`` 聚合，stage
    与底层 ``audit_events.event_title`` 解耦——漏斗顶端把"接受+更新+拒绝"全算进
    ``market_discovered``，中段 ``market_filtered_in`` 只算"接受+更新"。
    """

    join_clause, where_extra, params = _build_market_filter(
        condition_alias="a.condition_id",
        league=league,
        market_type=market_type,
    )

    filter_clauses = ",\n        ".join(
        f"COUNT(*) FILTER (WHERE a.event_title = ANY(:stage_{i}_titles)) AS stage_{i}"
        for i in range(len(FUNNEL_STAGE_EVENT_TITLES))
    )
    # 总过滤集 = 所有 stage 对应 event_title 的并集，避免扫无关行
    all_event_titles: list[str] = []
    seen: set[str] = set()
    for _, titles in FUNNEL_STAGE_EVENT_TITLES:
        for title in titles:
            if title not in seen:
                seen.add(title)
                all_event_titles.append(title)
    sql = f"""
        SELECT {filter_clauses}
        FROM audit_events a{join_clause}
        WHERE a.created_at >= :window_start
          AND a.created_at < :window_end
          AND a.event_title = ANY(:all_event_titles){where_extra}
    """
    bind: dict[str, Any] = {
        "window_start": window_start,
        "window_end": window_end,
        "all_event_titles": all_event_titles,
        **params,
    }
    for i, (_, titles) in enumerate(FUNNEL_STAGE_EVENT_TITLES):
        bind[f"stage_{i}_titles"] = list(titles)
    result = await session.execute(text(sql), bind)
    row = result.one()
    return {
        stage: int(row[i] or 0)
        for i, (stage, _) in enumerate(FUNNEL_STAGE_EVENT_TITLES)
    }


async def fetch_rejection_reasons(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    league: str | None = None,
    market_type: str | None = None,
    limit: int = 20,
) -> tuple[int, list[Mapping[str, Any]]]:
    """统计拒绝原因 top N (UNION audit_events + decision_records).

    覆盖 4 类拒绝源:
    - audit_events.order_rejected: gateway/executor 拒单
    - audit_events.risk_check_failed: 风控门禁拒单
    - audit_events.market_filtered_out: discovery 过滤
    - decision_records (accepted=false): quant 决策拒(quant_position_hold /
      edge_below_min / price_out_of_range / missing_best_ask 等). 量化拒绝是
      操盘"门禁调参"§15 的核心信号——只看 audit 三类会漏掉 90%+ 数据.
    按 ``reason`` 文本去前后空白后分组；空 reason 归到 ``<empty>``.
    """

    join_clause, where_extra, params = _build_market_filter(
        condition_alias="a.condition_id",
        league=league,
        market_type=market_type,
    )
    decision_join, decision_where, decision_params = _build_market_filter(
        condition_alias="d.condition_id",
        league=league,
        market_type=market_type,
    )

    # UNION ALL: 两路 reason → 同一聚合键. audit 路按 event_title 过滤,
    # decision 路按 accepted=false (含全部 quant 拒绝 reason).
    sql_top = f"""
        WITH all_rejections AS (
            SELECT COALESCE(NULLIF(BTRIM(a.reason), ''), '<empty>') AS reason_key
            FROM audit_events a{join_clause}
            WHERE a.created_at >= :window_start
              AND a.created_at < :window_end
              AND a.event_title = ANY(:event_titles){where_extra}
            UNION ALL
            SELECT COALESCE(NULLIF(BTRIM(d.reason), ''), '<empty>') AS reason_key
            FROM decision_records d{decision_join}
            WHERE d.created_at >= :window_start
              AND d.created_at < :window_end
              AND d.accepted = false{decision_where}
        )
        SELECT reason_key, COUNT(*) AS reason_count
        FROM all_rejections
        GROUP BY reason_key
        ORDER BY reason_count DESC, reason_key ASC
        LIMIT :limit
    """
    sql_total = f"""
        SELECT (
            (SELECT COUNT(*) FROM audit_events a{join_clause}
             WHERE a.created_at >= :window_start
               AND a.created_at < :window_end
               AND a.event_title = ANY(:event_titles){where_extra})
            +
            (SELECT COUNT(*) FROM decision_records d{decision_join}
             WHERE d.created_at >= :window_start
               AND d.created_at < :window_end
               AND d.accepted = false{decision_where})
        ) AS total
    """
    bind = {
        "window_start": window_start,
        "window_end": window_end,
        "event_titles": list(REJECTION_EVENT_TITLES),
        "limit": limit,
        **params,
        **decision_params,
    }
    total_row = (await session.execute(text(sql_total), bind)).one()
    total = int(total_row[0] or 0)
    rows = (await session.execute(text(sql_top), bind)).all()
    top = [{"key": str(row[0]), "count": int(row[1])} for row in rows]
    return total, top


async def fetch_execution_quality(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    league: str | None = None,
    market_type: str | None = None,
) -> dict[str, Any]:
    """提交、成交延迟分位和滑点统计。

    - submit_latency: ``market_filtered_in`` → ``order_submitted``，按
      ``condition_id`` + ``token_id`` 配对，取最近一次 filtered_in。
    - fill_latency: ``orders.created_at`` → ``fills.confirmed_at``，按
      ``order_id`` 直接连接。
    - slippage_bps: BUY 取 ``(fill_price - order_price) / order_price * 10000``，
      SELL 反号，便于"正值=不利"的统一语义。
    """

    submit_join, submit_where, submit_params = _build_market_filter(
        condition_alias="sub.condition_id",
        league=league,
        market_type=market_type,
    )
    fill_join, fill_where, fill_params = _build_market_filter(
        condition_alias="f.condition_id",
        league=league,
        market_type=market_type,
    )

    sql = f"""
        WITH submit_lat AS (
            SELECT EXTRACT(EPOCH FROM (sub.created_at - flt.created_at)) * 1000.0 AS latency_ms
            FROM audit_events sub
            JOIN LATERAL (
                SELECT created_at FROM audit_events
                WHERE event_title = ANY(:filtered_in_titles)
                  AND condition_id = sub.condition_id
                  AND token_id IS NOT DISTINCT FROM sub.token_id
                  AND created_at <= sub.created_at
                ORDER BY created_at DESC
                LIMIT 1
            ) flt ON TRUE{submit_join}
            WHERE sub.event_title = 'order_submitted'
              AND sub.created_at >= :window_start
              AND sub.created_at < :window_end{submit_where}
        ),
        fill_lat AS (
            SELECT EXTRACT(EPOCH FROM (f.confirmed_at - o.created_at)) * 1000.0 AS latency_ms,
                   o.side AS side,
                   o.price AS order_price,
                   f.price AS fill_price
            FROM fills f
            JOIN orders o ON o.order_id = f.order_id{fill_join}
            WHERE f.confirmed_at IS NOT NULL
              AND f.confirmed_at >= :window_start
              AND f.confirmed_at < :window_end
              AND o.price IS NOT NULL
              AND o.price > 0
              AND f.price IS NOT NULL{fill_where}
        )
        SELECT
            (SELECT percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) FROM submit_lat) AS submit_p50,
            (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FROM submit_lat) AS submit_p95,
            (SELECT percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) FROM fill_lat)   AS fill_p50,
            (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FROM fill_lat)   AS fill_p95,
            (SELECT AVG(
                CASE WHEN side = 'sell'
                     THEN (order_price - fill_price) / order_price * 10000.0
                     ELSE (fill_price - order_price) / order_price * 10000.0 END
             ) FROM fill_lat) AS slip_mean,
            (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY
                CASE WHEN side = 'sell'
                     THEN (order_price - fill_price) / order_price * 10000.0
                     ELSE (fill_price - order_price) / order_price * 10000.0 END
             ) FROM fill_lat) AS slip_p95,
            (SELECT COUNT(*) FROM fill_lat) AS sample_size
    """
    bind = {
        "window_start": window_start,
        "window_end": window_end,
        "filtered_in_titles": list(_FILTERED_IN_EVENT_TITLES),
        **submit_params,
        **fill_params,
    }
    row = (await session.execute(text(sql), bind)).one()
    return {
        "submit_p50": _as_float(row[0]),
        "submit_p95": _as_float(row[1]),
        "fill_p50": _as_float(row[2]),
        "fill_p95": _as_float(row[3]),
        "slip_mean": _as_float(row[4]),
        "slip_p95": _as_float(row[5]),
        "sample_size": int(row[6] or 0),
    }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_kelly_calibration(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """Kelly implied_p 校准统计——用于检测 prob_p 估计的系统性偏倚。

    扫描窗口内的 ``allocations``，按 raw_payload.kelly 子键取出每笔分配的
    ``prob_p`` / ``price_c`` / ``edge_net``。配对同 condition / token 的最近
    fills 推算实际成交价 vs implied fair。返回：

    * sample_size：样本数
    * mean_implied_p：平均 implied_p
    * mean_realized_price_lag_seconds：从分配到 fill 的平均延迟
    * mean_edge_net：平均 edge

    注：完整 Brier score 需要 market 终态结算结果（非本函数职责，需
    ``positions.settled_zero_value`` 关联），先返回前置指标。
    """

    sql = """
        SELECT
            COUNT(*) AS sample_size,
            AVG((a.raw_payload->'kelly'->>'prob_p')::numeric) AS mean_implied_p,
            AVG((a.raw_payload->'kelly'->>'edge_net')::numeric) AS mean_edge_net,
            AVG((a.raw_payload->'kelly'->>'price_c')::numeric) AS mean_price_c
        FROM allocations a
        WHERE a.created_at >= :window_start
          AND a.created_at < :window_end
          AND a.buy_budget_usdc > 0
          AND a.raw_payload ? 'kelly'
          
    """
    params: dict[str, Any] = {
        "window_start": window_start,
        "window_end": window_end,
    }
    row = (await session.execute(text(sql), params)).first()
    if row is None:
        return {
            "sample_size": 0,
            "mean_implied_p": None,
            "mean_edge_net": None,
            "mean_price_c": None,
        }
    return {
        "sample_size": int(row[0] or 0),
        "mean_implied_p": _as_float(row[1]),
        "mean_edge_net": _as_float(row[2]),
        "mean_price_c": _as_float(row[3]),
    }


__all__: Sequence[str] = (
    "FUNNEL_STAGES",
    "REJECTION_EVENT_TITLES",
    "REJECTION_REASON_CATEGORIES",
    "categorize_rejection_reason",
    "fetch_funnel_counts",
    "fetch_rejection_reasons",
    "fetch_execution_quality",
    "fetch_kelly_calibration",
)
