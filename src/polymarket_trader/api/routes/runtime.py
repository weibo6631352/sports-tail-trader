from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from polymarket_trader.api.aggregators import (
    AnalyticsAggregator,
    ReconcileDecisionsAggregator,
    RuntimeAggregator,
)
from polymarket_trader.api.deps import build_time_range, get_runtime

router = APIRouter(tags=["runtime"])

_DECISIONS_DUMP_DEFAULT_LIMIT = 1000
_DECISIONS_DUMP_MAX_LIMIT = 10000


@router.get("/runtime")
async def runtime(runtime: Any = Depends(get_runtime)) -> dict[str, object]:
    aggregator = RuntimeAggregator(runtime=runtime, session_factory=runtime.db_session_factory)
    return await aggregator.runtime_snapshot()


@router.get("/markets/tracking-breakdown")
async def markets_tracking_breakdown(
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """诊断: tracked markets 按多维度拆开统计.

    用于对照 polymarket /sports/live: 我们 tracked N 个 markets 但 polymarket
    可能 0 个 live, 看哪些维度上不一致.

    返回:
    - total_registry / total_ws_tracked / total_entry_metadata
    - by_sport: 按 sport 分布 (按 market_slug 前缀推断)
    - by_trading_status: ELIGIBLE / PAUSED / CLOSED / RESOLVED
    - by_trade_window: 按 game_start_time 与当前时间关系分类
        * no_game_start: outright/futures
        * upcoming_30min: 即将开赛 (now ~ now+30min)
        * upcoming_far: 远期未开赛 (>30min 后)
        * in_progress: 正在比赛窗口 (now-6h ~ now)
        * elapsed: 已结束 (game_start < now-6h)
    - by_live_state: 是否在 entry_metadata 中有 live_state_payload
    - sample_elapsed: 最早结束的 20 个 market (应该被 prune 但没的样本)
    """
    from datetime import datetime, timedelta, timezone
    from collections import Counter

    registry = getattr(runtime, "registry", None)
    if registry is None:
        return {"available": False, "reason": "registry_unavailable"}

    ws_worker = getattr(runtime, "market_ws_worker", None)
    metadata_store = getattr(runtime, "market_metadata_store", None)

    snapshot = registry.snapshot()
    markets = snapshot.markets
    now = datetime.now(timezone.utc)
    window_future = timedelta(minutes=30)
    window_past = timedelta(hours=6)

    by_sport: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    by_window: Counter[str] = Counter()
    by_live_phase: Counter[str] = Counter()
    elapsed_samples: list[dict[str, Any]] = []

    ws_tracked_tokens = set(getattr(ws_worker, "_tracked_markets", {}).keys()) if ws_worker else set()
    # 真正经过 should_subscribe_ws gate 通过、会被订阅的 token 数。
    # 比 total_ws_tracked_tokens（registry × 2）更准确——前端"WS 订阅"指标用这个。
    from polymarket_trader.runtime.ws_loops import market_ws_subscription_token_ids
    try:
        subscribed_token_ids = market_ws_subscription_token_ids(runtime)
    except Exception:
        subscribed_token_ids = ()

    # 预构建 metadata 索引：cid → record，用于 phase 分布统计
    metadata_by_cid: dict[str, Any] = {}
    if metadata_store:
        for r in metadata_store.records():
            if r.condition_id:
                metadata_by_cid[r.condition_id] = r

    for market in markets:
        # by_sport: 按 slug 前缀 (粗略)
        slug = (market.market_slug or "").lower()
        sport = "unknown"
        for s in ("mlb", "nba", "wnba", "nhl", "nfl", "ncaaf", "ncaab", "kbo", "cricket",
                  "atp", "wta", "itf", "mls", "epl", "laliga", "j1100", "j2100", "j3",
                  "cs2", "lol", "dota", "val", "sc2", "ow", "esports",
                  "boxing", "mma", "ufc", "f1", "motogp", "golf", "tennis"):
            if slug.startswith(s + "-") or slug.startswith(s + "1-") or slug.startswith(s + "2-"):
                sport = s
                break
        if sport == "unknown" and slug:
            # 取第一段 (xxx-yyy-zzz → xxx)
            sport = slug.split("-")[0] if "-" in slug else slug[:10]
        by_sport[sport] += 1

        # by_trading_status
        ts = getattr(market.trading_status, "value", str(market.trading_status))
        by_status[ts] += 1

        # by_trade_window
        gst = market.game_start_time
        if gst is None:
            window_key = "no_game_start"
        else:
            if gst.tzinfo is None:
                gst = gst.replace(tzinfo=timezone.utc)
            if gst > now + window_future:
                window_key = "upcoming_far"
            elif gst > now:
                window_key = "upcoming_30min"
            elif gst > now - window_past:
                window_key = "in_progress"
            else:
                window_key = "elapsed"
                if len(elapsed_samples) < 20:
                    elapsed_samples.append({
                        "market_slug": market.market_slug,
                        "condition_id": market.condition_id,
                        "trading_status": ts,
                        "game_start_time": gst.isoformat(),
                        "elapsed_minutes": int((now - gst).total_seconds() / 60),
                    })
        by_window[window_key] += 1

        # by_live_phase: live_state_worker 写入的 entry_metadata.live_state_phase 分布
        rec = metadata_by_cid.get(market.condition_id)
        phase = (getattr(rec, "live_state_phase", None) or "no_metadata") if rec else "no_metadata"
        by_live_phase[phase] += 1

    return {
        "available": True,
        "total_registry": len(markets),
        "total_ws_tracked_tokens": len(ws_tracked_tokens),
        "total_entry_metadata": len(metadata_by_cid),
        # 真实订阅 token 数——已通过 should_subscribe_ws gate
        # （phase=live + signal_allowed + status=eligible 或 exposure override）。
        "ws_subscribed_token_count": len(subscribed_token_ids),
        "by_sport": dict(by_sport.most_common(30)),
        "by_trading_status": dict(by_status),
        "by_trade_window": dict(by_window),
        "by_live_phase": dict(by_live_phase),
        "elapsed_samples": elapsed_samples,
        "note": "ws_subscribed_token_count = should_subscribe_ws gate 通过的真实订阅数。"
                "对照 https://polymarket.com/zh/sports/live",
    }


@router.get("/workers")
async def workers(runtime: Any = Depends(get_runtime)) -> dict[str, object]:
    return RuntimeAggregator(runtime=runtime).workers_snapshot()


@router.get("/metrics")
async def metrics(runtime: Any = Depends(get_runtime)) -> dict[str, object]:
    return RuntimeAggregator(runtime=runtime).metrics_snapshot()


@router.get("/metrics/latency-percentiles")
async def metrics_latency_percentiles(
    window_ms: int | None = Query(default=None, ge=0),
    sample_limit: int = Query(default=500, ge=1, le=5000),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """订单执行 latency 分位数（queue→sign / sign→submit / submit→ack / queue→ack）。"""

    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.latency_percentiles_snapshot(
        window_ms=window_ms,
        sample_limit=sample_limit,
    )


@router.get("/admin/decisions/dump")
async def dump_decision_records(
    limit: int = Query(default=_DECISIONS_DUMP_DEFAULT_LIMIT, ge=1, le=_DECISIONS_DUMP_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    accepted: bool | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """从 ``decision_records`` 表分页查询历史决策。"""

    aggregator = ReconcileDecisionsAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.list_decisions(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        condition_id=condition_id,
        accepted=accepted,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/decisions/{record_id}")
async def get_decision_record(
    record_id: str,
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """按 ``record_id`` 取单条决策详情。"""

    aggregator = ReconcileDecisionsAggregator(session_factory=runtime.db_session_factory)
    payload = await aggregator.get_decision_record(record_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="decision_record_not_found")
    return payload
