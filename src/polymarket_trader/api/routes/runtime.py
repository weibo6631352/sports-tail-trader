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


@router.get("/runtime/build-signature")
async def runtime_build_signature(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """R8 (CPO Round 5)：introspect 当前 binary 是否含 R6/R7 关键改动。

    用户重启后调一次即可确认部署生效（背景：用户两次跳过 R5/R6/R7 验证，导致
    后端可能仍跑旧 binary——这个端点主动暴露"代码已改运行时未更新"的事实，
    杜绝静默漂移）。

    返回特征：
    - ``signal_snapshot_module_present``：``domain/signal_snapshot.py`` 能否
      import（R6 新建文件，旧 binary 没有）
    - ``live_signal_snapshot_field_count``：``LiveSignalSnapshot`` dataclass
      字段数（R6 设计 11；其它值 = R6 未完整加载）
    - ``quant_decider_prob_provider_via_estimate_signal``：R7 改造后
      ``size_entry`` 闭包 ``_prob_provider`` 走 estimate_signal 单一入口的
      静态检测（旧 binary 是手写 candidates max）
    - ``signal_snapshot_store_present``：``RuntimeComponents.signal_snapshot_store``
      字段是否实例化（R6 wiring）

    任一关键特征缺失 → ``runtime_outdated=True`` + ``advisory`` 列出缺什么。
    端点本身永远 200 OK；用 ``runtime_outdated`` 字段给 operator 决策依据，
    不强行 raise 503 避免误伤 healthcheck。
    """
    advisory: list[str] = []
    signature: dict[str, Any] = {}

    # 特征 1: signal_snapshot module
    try:
        from polymarket_trader.domain.signal_snapshot import LiveSignalSnapshot
        import dataclasses as _dc
        field_count = len(_dc.fields(LiveSignalSnapshot))
        signature["signal_snapshot_module_present"] = True
        signature["live_signal_snapshot_field_count"] = field_count
        # R15 加 account_age_s 字段 → 期望从 11 → 12（R6 11 + R15 1）
        if field_count != 12:
            advisory.append(
                f"LiveSignalSnapshot field_count={field_count} (expected 12) - R6/R15 partial load"
            )
    except ImportError:
        signature["signal_snapshot_module_present"] = False
        signature["live_signal_snapshot_field_count"] = None
        advisory.append("domain.signal_snapshot module missing - R6 not deployed")

    # 特征 2: quant_decider 走 estimate_signal 单一入口（R7）
    try:
        import inspect as _inspect
        from polymarket_trader.workflow import quant_decider as _qd
        size_entry_source = _inspect.getsource(_qd.size_entry)
        # R7 后 _prob_provider 调 estimate_signal；R7 前调 math_prob+goalserve_prob
        uses_estimate_signal = "estimate_signal(" in size_entry_source
        uses_legacy_calls = (
            "math_prob(context," in size_entry_source
            or "goalserve_prob(context," in size_entry_source
        )
        signature["quant_decider_prob_provider_via_estimate_signal"] = uses_estimate_signal
        signature["quant_decider_prob_provider_legacy_calls"] = uses_legacy_calls
        if not uses_estimate_signal or uses_legacy_calls:
            advisory.append(
                "quant_decider._prob_provider not on estimate_signal single-entry - R7 not deployed"
            )
    except Exception as exc:  # noqa: BLE001
        signature["quant_decider_prob_provider_via_estimate_signal"] = None
        advisory.append(f"quant_decider introspect failed: {str(exc)[:80]}")

    # 特征 3: RuntimeComponents.signal_snapshot_store 字段已实例化（R6 wiring）
    # R18 (Code Q1): RuntimeComponents.signal_snapshot_store 非 Optional，runtime 由
    # fastapi Depends(get_runtime) 注入也非 None，直接 .signal_snapshot_store 安全。
    # 但保留 advisory 路径——hasattr token_count 这种 quack 容错给 hot-swap 场景兜底。
    store = runtime.signal_snapshot_store
    signature["signal_snapshot_store_present"] = True
    signature["signal_snapshot_store_token_count"] = (
        store.token_count() if hasattr(store, "token_count") else None
    )

    signature["runtime_outdated"] = len(advisory) > 0
    signature["advisory"] = advisory
    return signature


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

    # R18: RuntimeComponents 强类型直接访问；3 字段都非 Optional，runtime 自身由
    # Depends(get_runtime) 保证非 None（unavailable 时 get_runtime 已返 503）。
    registry = runtime.registry
    ws_worker = runtime.market_ws_worker
    metadata_store = runtime.market_metadata_store

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
        # by_sport：单一 sport_resolver 权威映射（参 sports/slug_resolver.py）；
        # market.sport 由 adapter 出口回填，此处直接读，避免在健康面板再写一份
        # 硬编码 slug→sport 列表（R5-C 反冗余收口）。
        sport = market.sport or "unknown"
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


@router.get("/system/gc-types-top")
async def system_gc_types_top(limit: int = 30) -> dict[str, Any]:
    """R37 leak hunt: gc.get_objects() type 计数 + 总字节估算.

    heap dump 揭示 565K × 320 字节对象 = 181 MB; tracemalloc 看不到对象 type, 这里
    用 gc 走 Python heap 全部对象 + collections.Counter 按 type 名称汇总, 找具体
    哪个类持有大量实例 (Market / Dict / OrderbookSnapshot / ...).
    """
    import asyncio
    import gc as _gc
    import sys as _sys
    from collections import Counter

    def _scan() -> dict[str, Any]:
        objs = _gc.get_objects()
        type_counts: Counter[str] = Counter()
        type_size_estimate: dict[str, int] = {}
        for obj in objs:
            tname = type(obj).__name__
            type_counts[tname] += 1
            # 估 size 用 sys.getsizeof (浅 size 不含递归引用)
            if tname not in type_size_estimate:
                try:
                    type_size_estimate[tname] = _sys.getsizeof(obj)
                except Exception:
                    type_size_estimate[tname] = 0
        top = type_counts.most_common(max(1, min(limit, 100)))
        return {
            "total_objects": len(objs),
            "top": [
                {
                    "type": tname,
                    "count": count,
                    "avg_size_bytes": type_size_estimate.get(tname, 0),
                    "estimated_total_mb": round(count * type_size_estimate.get(tname, 0) / 1024 / 1024, 2),
                }
                for tname, count in top
            ],
        }

    return await asyncio.to_thread(_scan)


@router.get("/system")
async def system_perf(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """系统级运行状态——CPU / 内存 / DB pool / 工作流阶段耗时 / 队列水位.

    SystemPerfMonitor.snapshot() 一次性返回(全部 in-memory,无 DB):
    - process_metrics: pid / threads / RSS / VMS / cpu_percent / open_files / fd
    - system_metrics: cpu_count / load_avg / memory_used_pct / disk / net_io
    - asyncio_metrics: task_count / running_task_names
    - worker_frequencies: 每个 scheduler/worker 实际 tick rate vs 期望 drift
    - event_latencies: 各 DomainEventType 端到端 P50/P90/P99 (queue→handle)
    - decision_pipeline_latencies: 决策链路各 step 耗时分位
    - memory_growth + pnl_drift + gc_stats: 长期跑泄漏/稳定性诊断
    - http_endpoints: 每个 endpoint 调用数/error_rate/latency/throughput
    - ws_traffic + ws_queues + db_query 分位: 协议/存储压力
    - db_pool: SQLAlchemy 连接池 size/checkedin/checkedout/overflow

    实时(non-cached). 调用成本 ~10-50ms (psutil syscalls + 内存遍历).
    """
    from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor

    snapshot = SystemPerfMonitor.get().snapshot()
    # 补充 DB pool stats (SystemPerfMonitor 内不持有 engine 引用,只能在路由层取)
    db_pool: dict[str, Any] = {}
    try:
        # R18: runtime.db_session_factory 非 Optional；factory.kw / .bind 仍 getattr（SqlAlchemy 兼容路径）
        factory = runtime.db_session_factory
        if factory is not None:
            engine = getattr(factory, "kw", {}).get("bind") or getattr(factory, "bind", None)
            if engine is None:
                # async_sessionmaker.kw / .bind 命名版本差异;再兜底拿 sync_engine
                sync_eng = getattr(engine, "sync_engine", None) if engine else None
                engine = sync_eng or engine
            pool = getattr(engine, "pool", None) or getattr(getattr(engine, "sync_engine", None), "pool", None)
            if pool is not None:
                db_pool = {
                    "size": pool.size(),
                    "checked_in": pool.checkedin(),
                    "checked_out": pool.checkedout(),
                    "overflow": pool.overflow(),
                    "status": pool.status(),
                }
    except Exception as exc:  # noqa: BLE001
        db_pool = {"error": str(exc)[:120]}
    snapshot["db_pool"] = db_pool
    # 内存 store 大小(buffer 容量 / bucket 数 / outbox 状态)
    # R18: 5 个 store 字段都是 RuntimeComponents 非 Optional，runtime 由 Depends 注入非 None。
    # try/except 块保留兜底 store 内部方法异常，但属性访问直接 .X（IDE 能查重命名）。
    in_memory_stores: dict[str, Any] = {}
    try:
        in_memory_stores["sports_live_history_tracked_conditions"] = (
            runtime.sports_live_history_buffer.tracked_condition_count()
        )
    except Exception: pass
    try:
        ob_buf = runtime.orderbook_history_buffer
        in_memory_stores["orderbook_history_tracked_tokens"] = (
            ob_buf.tracked_token_count() if hasattr(ob_buf, "tracked_token_count") else None
        )
    except Exception: pass
    try:
        in_memory_stores["registry_markets"] = len(runtime.registry.snapshot().markets)
    except Exception: pass
    try:
        in_memory_stores["market_metadata_records"] = len(list(runtime.market_metadata_store.records()))
    except Exception: pass
    snapshot["in_memory_stores"] = in_memory_stores
    return snapshot


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
