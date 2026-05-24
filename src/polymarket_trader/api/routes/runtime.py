from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from polymarket_trader.api.deps import build_time_range, get_admin_service, get_runtime
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(tags=["runtime"])

_DECISIONS_DUMP_DEFAULT_LIMIT = 1000
_DECISIONS_DUMP_MAX_LIMIT = 10000


@router.get("/runtime")
async def runtime(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return await service.runtime_snapshot()


@router.get("/runtime/paper-ledger")
async def paper_ledger_snapshot(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """Paper trading 虚拟账本快照——只在 PAPER_TRADING_MODE=true 时有数据。

    暴露:
    - available_usdc: 当前可用虚拟余额（初始 portfolio_budget_usdc - 已花费 + SELL 回款）
    - positions: {token_id: net_shares}（已扣 fee_shares）
    - cost_basis_usdc: {token_id: 累计 gross cost}（算 avg_price 用）
    - fees_accrued_usdc: 累计 fee 总额
    - 派生: avg_prices, unrealized_pnl_usdc（按当前 best_bid mark-to-market）
    """
    snapshot = service.paper_ledger_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="paper_trading_mode not enabled")
    return snapshot


@router.get("/runtime/risk-metrics")
async def risk_metrics(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """量化风险度量（Sharpe / Sortino / Calmar / VaR 95-99）。

    基于 equity_curve 时序（每分钟 1 点）计算：
    - Sharpe ratio: risk-adjusted return
    - Sortino: downside-adjusted return
    - Calmar: 年化收益 / max drawdown
    - VaR 95/99: 历史 5%/1% 分位单分钟损失
    """
    d = service.risk_metrics_snapshot()
    if d is None: raise HTTPException(status_code=404, detail="paper_trading_mode not enabled")
    return d


@router.get("/runtime/anomalies")
async def anomalies(
    window_minutes: int = Query(default=5, ge=1, le=60),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """异常检测（reject reason 突变 / 死仓识别 / 集中度告警）。

    检查项：
    - reject_reason_spike: 某 reason N min 内触发 >=5 且对比前一窗口 >=3×
    - dead_position: 持仓时长 > 6h 且 best_bid=None
    - concentration_warning: 单 sport > 60% 仓位
    """
    return await service.anomalies_snapshot(window_minutes=window_minutes)


@router.get("/runtime/equity-curve")
async def equity_curve_snapshot(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """资金曲线时序（per minute, 最多 24h）—— 画图 + max drawdown 计算。

    每 60s recorder 写一条 {at, available, equity, positions_count, fees}。
    派生:
    - peak_equity / current_drawdown / max_drawdown
    - return_pct（首末点对比）
    - volatility_60min（最近 60 点 stdev）
    """
    snapshot = service.equity_curve_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="paper_trading_mode not enabled")
    return snapshot


@router.get("/runtime/trade-tape")
async def trade_tape_snapshot(
    condition_id: str = Query(..., description="Polymarket condition_id"),
    limit: int = Query(default=50, ge=1, le=500),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """Polymarket trade tape — 真实成交方向流量（非 OFI）。

    OFI 只算盘口变化（add/cancel），trade tape 是**实际成交方向**——
    谁是 taker（决定方向）。聚合：buy/sell notional 比、大单数、unique wallets、
    top whale wallets。15s 内的 cache 防止 admin 反复调爆 polymarket data-api。
    """
    return await service.market_trade_tape(condition_id=condition_id, limit=limit)


@router.get("/runtime/clv")
async def clv_snapshot(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """CLV (Closing Line Value) — 体育博彩黄金 KPI。

    入场后价格漂移分析：CLV > 0 = 入场抢到了先手（market 后续涨向我方）。
    long-term avg CLV > fee = 策略真正 +EV，不必等结算才知道好坏。

    数据:
    - open_positions_clv: 当前每个 open 仓位的 1m/5m/15m/30m CLV
    - closed_trades_clv: 已平仓的价格走势统计
    - summary.avg_current_clv_usdc: 平均当前 CLV
    """
    snapshot = service.clv_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="paper_trading_mode not enabled")
    return snapshot


@router.get("/runtime/mlb-playbyplay")
async def mlb_playbyplay_snapshot(
    game_id: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """MLB 逐球事件流（每球，仅观测）。paper 模式启用。"""
    snapshot = service.mlb_playbyplay_snapshot(game_id=game_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="MLB play-by-play not active")
    return snapshot


@router.get("/runtime/mlb-schedule")
async def mlb_schedule(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """MLB 全赛季 fixtures（lazy fetch + 1h cache）。"""
    d = await service.mlb_schedule_snapshot()
    if d is None: raise HTTPException(status_code=404, detail="goalserve lazy client not active")
    return d


@router.get("/runtime/mlb-standings")
async def mlb_standings(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """MLB 排名 + 强弱队 prior（lazy fetch + 1h cache）。"""
    d = await service.mlb_standings_snapshot()
    if d is None: raise HTTPException(status_code=404, detail="goalserve lazy client not active")
    return d


@router.get("/runtime/nba-standings")
async def nba_standings(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """NBA 排名（lazy fetch + 1h cache）。"""
    d = await service.nba_standings_snapshot()
    if d is None: raise HTTPException(status_code=404, detail="goalserve lazy client not active")
    return d


@router.get("/runtime/derived-metrics")
async def derived_metrics(
    market_slug: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """派生量化指标（基于已有时序数据计算高阶统计 / 特征）。

    每个 market 算: odds_volatility / drift_rate / market_efficiency / trend label。
    每个持仓算: holding_seconds / max_drawdown / price_trend / quality_score (0-100)。
    summary 含全局 avg/max。
    """
    return service.derived_metrics_snapshot(market_slug=market_slug)


@router.get("/runtime/error-rate")
async def error_rate(
    window_minutes: int = Query(default=15, ge=1, le=120),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """错误率时序（1min 桶聚合）+ 最近 1/5/15min 总错误率派生。

    用途：找错误暴增时段，告警阈值校准。
    """
    return await service.error_rate_timeseries(window_minutes=window_minutes)


@router.get("/runtime/data-staleness")
async def data_staleness(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """每个数据源的 lag/staleness — healthy 占比 + stale 列表 + per source 详情。"""
    return service.data_staleness_snapshot()


@router.get("/runtime/system-perf")
async def system_perf(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """系统性能 + 健康 + 启动耗时统一可观测视图。

    一次拿全：
    - boot_phase_timings: bootstrap 各阶段耗时（找启动慢点）
    - process_metrics: CPU/RSS/threads/connections/ctx_switches/io
    - system_metrics: CPU per core / load avg / memory / disk / net io
    - asyncio_metrics: 当前 task 数 + running task names
    - http_endpoints: top 30 by call_count + latency p50/p90/p99 + error rate
    - ws_traffic: per channel msg rate + bandwidth
    - db_queries: count + 延迟分位
    - db_pool: size/checked_in/checked_out/overflow
    - db_ping_ms: SELECT 1 实时延迟
    """
    return await service.system_perf_snapshot()


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
    metadata_store = getattr(runtime, "entry_metadata_store", None)

    snapshot = registry.snapshot()
    markets = snapshot.markets
    now = datetime.now(timezone.utc)
    window_future = timedelta(minutes=30)
    window_past = timedelta(hours=6)

    by_sport: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    by_window: Counter[str] = Counter()
    by_live_state: Counter[str] = Counter()
    elapsed_samples: list[dict[str, Any]] = []

    ws_tracked_tokens = set(getattr(ws_worker, "_tracked_markets", {}).keys()) if ws_worker else set()
    metadata_cids = (
        {r.condition_id for r in metadata_store.records() if r.condition_id}
        if metadata_store else set()
    )

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

        # by_live_state (按 cid 查 metadata_store)
        if market.condition_id in metadata_cids:
            by_live_state["has_metadata"] += 1
        else:
            by_live_state["no_metadata"] += 1

    return {
        "available": True,
        "total_registry": len(markets),
        "total_ws_tracked_tokens": len(ws_tracked_tokens),
        "total_entry_metadata": len(metadata_cids),
        "by_sport": dict(by_sport.most_common(30)),
        "by_trading_status": dict(by_status),
        "by_trade_window": dict(by_window),
        "by_live_state": dict(by_live_state),
        "elapsed_samples": elapsed_samples,
        "note": "对照 https://polymarket.com/zh/sports/live - in_progress 应该是真正 live, "
                "elapsed 是应被 prune 的, upcoming_far 是 discovery 拉了远期市场.",
    }


@router.get("/runtime/pipeline-health")
async def pipeline_health(window_seconds: float = 60.0) -> dict[str, object]:
    """每个管道/worker 的综合健康视图 + 内部 step 耗时分解.

    一次拿全 (按 share_of_total_cpu_pct 倒序):
    - health: healthy / degraded / unhealthy / idle (启发判断)
    - health_reasons: 触发降级的具体原因 (cpu_share/drift/error_rate)
    - cpu_avg_ms / cpu_p99_ms / wall_avg_ms / cpu_efficiency
    - share_of_total_cpu_pct: 占所有 pipeline CPU 总量比例
    - worker_tick: 期望/实际轮询间隔 + drift_pct (worker 类管道)
    - steps[]: 内部各 step 的 cpu_avg/p99 + share_of_pipeline_pct
    - error_count / error_rate_pct: 异常累计

    健康规则 (按严重度自上而下匹配):
    - idle:       60s 内无调用 (调用次数=0)
    - unhealthy:  error_rate > 10% OR cpu_share > 70% OR drift > 100%
    - degraded:   error_rate 1-10% OR cpu_share 30-70% OR drift > 30%
    - healthy:    其它

    建议工作流:
    1. 健康面板看每个 pipeline 状态.
    2. 红色 (unhealthy) 立即点开看 health_reasons 和 steps 定位.
    3. steps 中 share_of_pipeline_pct 高的就是优化点.
    """
    from polymarket_trader.runtime.system_perf_monitor import (
        SystemPerfMonitor, pipeline_health_summary,
    )
    monitor = SystemPerfMonitor.get()
    window = max(1.0, min(window_seconds, 3600.0))
    rows = pipeline_health_summary(
        cpu_samples_map=monitor.pipeline_cpu_samples,
        step_samples_map=monitor.pipeline_step_samples,
        worker_ticks=monitor.worker_ticks,
        coroutine_exceptions=monitor.coroutine_exceptions,
        window_seconds=window,
    )
    summary_by_health: dict[str, int] = {"healthy": 0, "degraded": 0, "unhealthy": 0, "idle": 0}
    for row in rows:
        h = row.get("health", "idle")
        summary_by_health[h] = summary_by_health.get(h, 0) + 1
    return {
        "window_seconds": window,
        "pipeline_count": len(rows),
        "summary_by_health": summary_by_health,
        "pipelines": rows,
    }


@router.get("/runtime/pipeline-cpu")
async def pipeline_cpu(window_seconds: float = 60.0) -> dict[str, object]:
    """按"管道/worker"维度的真实 CPU 占用 (用 ``time.process_time()`` 算).

    返回每个 pipeline 在最近 ``window_seconds`` (默认 60s) 内的:
    - calls / cpu_total_ms / cpu_avg_ms / cpu_p50_ms / cpu_p99_ms / cpu_max_ms
    - wall_total_ms / wall_avg_ms / calls_per_second
    - cpu_efficiency = cpu_total / wall_total, [0,1]:
        * 接近 1: 纯 CPU 工作 (如 derived 计算 / candidates 评估)
        * 接近 0: IO 密集 (如 goalserve HTTP fetch — wall 大但 cpu 几乎 0)
    - share_of_total_cpu_pct: 该 pipeline 占所有追踪 pipeline CPU 总量的百分比

    pipelines 按 share_of_total_cpu_pct 倒序, 一眼看出 CPU 大头. 用于:
    - 性能调优定位 (哪个 pipeline 吃 CPU)
    - 优化前后效果对比 (同 pipeline 改造前后 cpu_avg_ms 对比)
    - 健康监控 (突增表示某 pipeline 异常)
    """
    from polymarket_trader.runtime.system_perf_monitor import (
        SystemPerfMonitor,
        pipeline_cpu_summary,
    )
    monitor = SystemPerfMonitor.get()
    window = max(1.0, min(window_seconds, 3600.0))
    rows = pipeline_cpu_summary(monitor.pipeline_cpu_samples, window_seconds=window)
    return {
        "window_seconds": window,
        "pipeline_count": len(rows),
        "pipelines": rows,
    }


@router.get("/runtime/memory-timeseries")
async def memory_timeseries(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """进程 RSS 时间序列(每 30s 一点,保留 1h = 120 点)+ 增长率 + 泄漏判断."""
    return service.memory_timeseries_snapshot()


@router.get("/runtime/memory-components")
async def memory_components(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """各 runtime store/buffer 内存占用估算 — 定位"哪个 store 最大"."""
    return service.memory_components_snapshot()


@router.get("/runtime/memory-objects")
async def memory_objects(
    top_n: int = Query(default=30, ge=1, le=200),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """gc.get_objects() 按 type 分组 top N — 识别"哪类对象最多"."""
    return service.memory_objects_snapshot(top_n=top_n)


@router.get("/runtime/data-sources-health")
async def data_sources_health(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """所有数据源延迟 / 健康 / 错误的统一可观测视图。

    包括 goalserve（inplay/livescore/pregame/lazy/pbp）+ polymarket（market_ws/user_ws/
    trade_tape_cache）+ outbox 积压 + reconcile age + workers。
    一次拿全运维数据，定位"哪条数据链路慢/挂了"。
    """
    return service.data_sources_health()


@router.get("/runtime/odds-drift")
async def odds_drift(
    market_slug: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """Goalserve 赔率漂移时序（每 market 5s 采样，最多 200 点 ~16min）。

    - 不传 market_slug: 返回所有 tracked market 摘要
    - 含 market_slug: 完整时序 + 派生漂移率（ml_home/away/draw、totals over、spread home）
    - 用途: 庄家收紧信号（vig 突然 inflate）、概率趋势识别
    """
    return service.odds_drift_snapshot(market_slug=market_slug, limit=limit)


@router.get("/runtime/arbitrage")
async def arbitrage_snapshot(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """同 event 跨盘口套利检测 — 隐含概率和异常分析。

    扫描 registry 所有 markets，按 event_slug 分组，对每个 market 取所有 outcome
    的 best_ask 求和：
    - sum < 1.0 = 套利机会（买所有 outcome 必赚 vig）
    - sum > 1.2 = 异常分歧（市场极度混乱）
    - 1.0 < sum < 1.10 = 健康 vig（5-10%）
    """
    return service.arbitrage_snapshot()


@router.get("/runtime/soccer-injuries")
async def soccer_injuries(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """Soccer 全部联赛伤病列表（lazy fetch + 1h cache）。"""
    d = await service.soccer_injuries_snapshot()
    if d is None: raise HTTPException(status_code=404, detail="goalserve lazy client not active")
    return d


@router.get("/runtime/live-attention")
async def live_attention(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """同时 LIVE 比赛分布（注意力分散度）。多 LIVE = 信号噪音大。"""
    return service.live_attention_snapshot()


@router.get("/runtime/h2h")
async def h2h(
    team1_id: str = Query(...),
    team2_id: str = Query(...),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """两队历史对决（lazy fetch + 1h cache）。"""
    d = await service.h2h_snapshot(team1_id, team2_id)
    if d is None: raise HTTPException(status_code=404, detail="goalserve lazy client not active")
    return d


@router.get("/runtime/nba-playbyplay")
async def nba_playbyplay_snapshot(
    game_id: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """NBA 逐事件流（实时，仅观测）。paper 模式启用。"""
    snapshot = service.nba_playbyplay_snapshot(game_id=game_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="NBA play-by-play not active")
    return snapshot


@router.get("/runtime/win-rate")
async def win_rate_breakdown(
    window_hours: int = Query(default=168, ge=1, le=720),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """历史 BUY+SELL 配对胜率分组（per 价位区间）。

    基于 audit_events.fill_recorded 配对计算 realized PnL，按入场价位分组：
    - trade_count / win_count / winrate / avg_pnl / total_pnl / profit_factor
    - 自动决策核心数据：哪个价位区间真实有 edge / 哪个价位是亏损区
    - 长期窗口（默认 7 天）覆盖大多数比赛开赛-结算周期
    """
    return await service.win_rate_breakdown(window_hours=window_hours)


@router.get("/runtime/guard-stats")
async def guard_stats(
    window_minutes: int = Query(default=60, ge=1, le=1440),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """每个 reject reason 在过去 N 分钟触发次数（防御性入场守卫的实战效果度量）。

    与 /candidates reason 聚合区别：本接口跨 4 个 event_title 聚合
    （order_rejected + risk_rejection_recorded + allocation_decision_recorded + market_filtered_out），
    一次拿全所有"被拦截"路径，按频次降序。
    """
    return await service.guard_stats_snapshot(window_minutes=window_minutes)


@router.get("/runtime/paper-metrics")
async def paper_metrics_snapshot(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    """Paper trading 综合量化指标——一次拿全 PnL/胜率/守卫/撮合/盘口/进度。

    比 /runtime/paper-ledger 全：含 simulations 成功率分布、ws 订阅状态、
    goal 进度（initial $100 → target $1000，progress_pct）、每仓 unrealized_pnl。
    """
    snapshot = service.paper_metrics_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="paper_trading_mode not enabled")
    return snapshot


@router.get("/runtime/paper-orders")
async def paper_orders_snapshot(
    limit: int = Query(default=50, ge=1, le=500),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """Paper trading 最近 N 条订单请求 + 撮合详情（含 unfilled / fee / consumed_levels）。

    用于复盘"为什么这单成/不成":撮合 unfilled_amount_usdc > 0 = 卖盘没货,
    unfilled_size_shares > 0 = SELL 买盘没接,fee_usdc 异常 = fee 配置问题。
    """
    snapshot = service.paper_orders_snapshot(limit=limit)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="paper_trading_mode not enabled")
    return snapshot


@router.get("/workers")
async def workers(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return service.workers_snapshot()


@router.get("/metrics")
async def metrics(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return service.metrics_snapshot()


@router.get("/metrics/latency-percentiles")
async def metrics_latency_percentiles(
    window_ms: int | None = Query(default=None, ge=0),
    sample_limit: int = Query(default=500, ge=1, le=5000),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """订单执行 latency 分位数（queue→sign / sign→submit / submit→ack / queue→ack）。

    从 ``outbox_events.payload->timestamps`` 抽样最近 ``sample_limit`` 条 order
    lifecycle 事件，计算每个 stage 的 p50/p90/p95/p99 毫秒数；现网延迟劣化
    （签名变慢 / WS 卡顿）操盘观测刚需。
    """

    return await service.latency_percentiles_snapshot(
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
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """从 ``decision_records`` 表分页查询历史决策。

    DB 是该接口唯一真相来源；进程内存中不再维护 ring buffer，无 DB 时直接返回空集。
    """

    return await service.list_decisions(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        condition_id=condition_id,
        accepted=accepted,
        time_range=build_time_range(since=since, until=until),
        strategy_id=strategy_id,
    )


@router.get("/decisions/{record_id}")
async def get_decision_record(
    record_id: str,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """按 ``record_id`` 取单条策略决策详情。

    返回完整 ``decision_input`` / ``decision_output`` JSONB——含 ``fair_value``、
    ``entry_price_cap``、``kelly_fraction``、拒绝原因枚举等策略中间量，便于从
    trade timeline 点开后做根因追查。
    """

    payload = await service.get_decision_record(record_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="decision_record_not_found")
    return payload
