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
# market_service 在接受新市场时 emit ``market_discovered``，对已跟踪市场的更新 emit
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
    strategy_id: str | None = None,
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
    strategy_where = " AND a.strategy_id = :strategy_id" if strategy_id is not None else ""

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
          AND a.event_title = ANY(:all_event_titles){where_extra}{strategy_where}
    """
    bind: dict[str, Any] = {
        "window_start": window_start,
        "window_end": window_end,
        "all_event_titles": all_event_titles,
        **params,
    }
    if strategy_id is not None:
        bind["strategy_id"] = strategy_id
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
    strategy_id: str | None = None,
) -> tuple[int, list[Mapping[str, Any]]]:
    """统计拒绝原因 top N。

    覆盖 ``order_rejected``、``risk_check_failed``、``market_filtered_out`` 三类事件，
    按 ``reason`` 文本去前后空白后分组；空 reason 归到 ``<empty>``。
    """

    join_clause, where_extra, params = _build_market_filter(
        condition_alias="a.condition_id",
        league=league,
        market_type=market_type,
    )
    strategy_where = " AND a.strategy_id = :strategy_id" if strategy_id is not None else ""

    sql_top = f"""
        SELECT COALESCE(NULLIF(BTRIM(a.reason), ''), '<empty>') AS reason_key,
               COUNT(*) AS reason_count
        FROM audit_events a{join_clause}
        WHERE a.created_at >= :window_start
          AND a.created_at < :window_end
          AND a.event_title = ANY(:event_titles){where_extra}{strategy_where}
        GROUP BY reason_key
        ORDER BY reason_count DESC, reason_key ASC
        LIMIT :limit
    """
    sql_total = f"""
        SELECT COUNT(*) AS total
        FROM audit_events a{join_clause}
        WHERE a.created_at >= :window_start
          AND a.created_at < :window_end
          AND a.event_title = ANY(:event_titles){where_extra}{strategy_where}
    """
    bind = {
        "window_start": window_start,
        "window_end": window_end,
        "event_titles": list(REJECTION_EVENT_TITLES),
        "limit": limit,
        **params,
    }
    if strategy_id is not None:
        bind["strategy_id"] = strategy_id
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
    strategy_id: str | None = None,
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
    submit_strategy_where = " AND sub.strategy_id = :strategy_id" if strategy_id is not None else ""
    fill_strategy_where = " AND f.strategy_id = :strategy_id" if strategy_id is not None else ""

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
              AND sub.created_at < :window_end{submit_where}{submit_strategy_where}
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
              AND f.price IS NOT NULL{fill_where}{fill_strategy_where}
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
    if strategy_id is not None:
        bind["strategy_id"] = strategy_id
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


__all__: Sequence[str] = (
    "FUNNEL_STAGES",
    "REJECTION_EVENT_TITLES",
    "fetch_funnel_counts",
    "fetch_rejection_reasons",
    "fetch_execution_quality",
)
