"""SystemObservabilityAggregator —— 进程/系统/数据源/内存/注意力等运维可观测。

7 类查询都基于 runtime 内部 store 实时聚合，不走 DB（除 DB ping latency 这一项
显式探测 DB 状态）。这些是 dashboard / 排障核心视图。

# 方法

- `system_perf_snapshot()` —— 全系统性能（CPU/RSS/asyncio/HTTP latency 分位/DB
  pool/event_bus 队列/outbox 积压/DB ping breakdown）
- `memory_timeseries_snapshot()` —— 进程 RSS 时序 + leak 嫌疑判定
- `memory_components_snapshot()` —— 各 runtime store 内存估算
- `memory_objects_snapshot(top_n)` —— gc.get_objects() type 分布
- `data_staleness_snapshot()` —— 各数据源 last_message / lag_seconds
- `data_sources_health()` —— 数据源 + 直播 + 盘口新鲜度桶分布
- `live_attention_snapshot()` —— market_ws tracked 市场的 sport 分布（注意力分散度）
"""

from __future__ import annotations

import gc
import logging
import time as _time_module
from collections import Counter
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class SystemObservabilityAggregator:
    def __init__(self, *, runtime: Any) -> None:
        self._runtime = runtime

    # ===== system_perf =====
    async def system_perf_snapshot(self) -> dict[str, object]:
        """全系统性能 + DB 健康 + 启动耗时统一可观测视图。"""

        from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor

        perf = SystemPerfMonitor.get()
        snapshot = perf.snapshot()
        runtime = self._runtime

        # DB pool 健康
        db_pool: dict[str, Any] = {}
        if runtime and runtime.db_engine:
            try:
                pool = runtime.db_engine.pool
                db_pool = {
                    "pool_class": type(pool).__name__,
                    "size": getattr(pool, "size", lambda: None)(),
                    "checked_in": getattr(pool, "checkedin", lambda: None)(),
                    "checked_out": getattr(pool, "checkedout", lambda: None)(),
                    "overflow": getattr(pool, "overflow", lambda: None)(),
                }
            except Exception as exc:  # noqa: BLE001
                db_pool = {"error": str(exc)}
        snapshot["db_pool"] = db_pool

        # Executor pools（trading / maintenance thread + process pool）
        executor_status: dict[str, Any] = {}
        for attr, label in (
            ("trading_thread_pool", "trading_thread_pool"),
            ("maintenance_thread_pool", "maintenance_thread_pool"),
        ):
            try:
                tp = getattr(runtime, attr, None)
                if tp:
                    entry: dict[str, Any] = {
                        "max_workers": tp._max_workers,
                        "active_threads": len(tp._threads) if hasattr(tp, "_threads") else None,
                        "queued_tasks": (
                            tp._work_queue.qsize() if hasattr(tp, "_work_queue") else None
                        ),
                    }
                    if attr == "trading_thread_pool":
                        entry["shutdown"] = tp._shutdown
                    executor_status[label] = entry
            except Exception as exc:  # noqa: BLE001
                executor_status[label] = {"error": str(exc)}
        try:
            pp = getattr(runtime, "maintenance_process_pool", None)
            if pp:
                executor_status["maintenance_process_pool"] = {
                    "max_workers": pp._max_workers,
                    "active_processes": (
                        len(pp._processes) if hasattr(pp, "_processes") else None
                    ),
                    "pending_work_items": (
                        len(pp._pending_work_items)
                        if hasattr(pp, "_pending_work_items") else None
                    ),
                }
        except Exception as exc:  # noqa: BLE001
            executor_status["maintenance_process_pool"] = {"error": str(exc)}
        snapshot["executor_pools"] = executor_status

        # Event bus 队列水位
        if runtime.event_bus and hasattr(runtime.event_bus, "snapshot"):
            try:
                qs = runtime.event_bus.snapshot()
                snapshot["event_bus_queues"] = {
                    "trading_depth": qs.trading_queue_depth,
                    "trading_capacity": qs.trading_queue_capacity,
                    "trading_util_pct": (
                        round(qs.trading_queue_depth / qs.trading_queue_capacity * 100, 1)
                        if qs.trading_queue_capacity else 0
                    ),
                    "maintenance_depth": qs.maintenance_queue_depth,
                    "maintenance_capacity": qs.maintenance_queue_capacity,
                    "persistence_depth": qs.persistence_queue_depth,
                    "persistence_capacity": qs.persistence_queue_capacity,
                    "low_priority_paused": qs.low_priority_paused,
                }
                if hasattr(runtime.event_bus, "mirror_failure_count"):
                    snapshot["event_bus_queues"]["mirror_failure_count"] = (
                        runtime.event_bus.mirror_failure_count()
                    )
            except Exception as exc:  # noqa: BLE001
                snapshot["event_bus_queues"] = {"error": str(exc)}

        # Outbox 真实 pending
        if runtime.outbox and hasattr(runtime.outbox, "snapshot"):
            try:
                ready, retained, dead = runtime.outbox.snapshot()
                snapshot["outbox"] = {
                    "ready_count": ready,
                    "retained_count": retained,
                    "dead_letter_count": dead,
                }
            except Exception as exc:  # noqa: BLE001
                snapshot["outbox"] = {"error": str(exc)}

        # DB ping latency — 拆 pool checkout / execute / commit 三段
        if runtime and runtime.db_session_factory:
            from sqlalchemy import text as _text
            try:
                t0 = _time_module.perf_counter()
                session_ctx = runtime.db_session_factory()
                session = await session_ctx.__aenter__()
                t_checkout = (_time_module.perf_counter() - t0) * 1000
                t1 = _time_module.perf_counter()
                await session.execute(_text("SELECT 1"))
                t_execute = (_time_module.perf_counter() - t1) * 1000
                t2 = _time_module.perf_counter()
                await session_ctx.__aexit__(None, None, None)
                t_close = (_time_module.perf_counter() - t2) * 1000
                total = (_time_module.perf_counter() - t0) * 1000
                snapshot["db_ping_ms"] = round(total, 2)
                snapshot["db_ping_breakdown"] = {
                    "pool_checkout_ms": round(t_checkout, 2),
                    "execute_ms": round(t_execute, 2),
                    "session_close_ms": round(t_close, 2),
                }
            except Exception as exc:  # noqa: BLE001
                snapshot["db_ping_ms"] = None
                snapshot["db_ping_error"] = str(exc)
        return snapshot

    # ===== memory_timeseries =====
    def memory_timeseries_snapshot(self) -> dict[str, object]:
        """进程 RSS 时间序列（每 30s 一点，保留 1h = 120 点）+ 派生指标。"""

        from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor

        ts = SystemPerfMonitor.get().memory_timeseries
        if not ts:
            return {"samples": 0, "points": []}
        points = [{"ts": float(t), "rss_mb": round(v, 1)} for t, v in ts]
        rss_values = [p["rss_mb"] for p in points]
        first_ts, last_ts = points[0]["ts"], points[-1]["ts"]
        duration_s = max(1.0, last_ts - first_ts)
        delta_mb = points[-1]["rss_mb"] - points[0]["rss_mb"]
        growth_mb_per_h = round(delta_mb / duration_s * 3600, 2)
        return {
            "samples": len(points),
            "duration_minutes": round(duration_s / 60, 1),
            "points": points,
            "first_rss_mb": points[0]["rss_mb"],
            "last_rss_mb": points[-1]["rss_mb"],
            "delta_mb": round(delta_mb, 2),
            "min_rss_mb": min(rss_values),
            "max_rss_mb": max(rss_values),
            "avg_rss_mb": round(sum(rss_values) / len(rss_values), 1),
            "growth_rate_mb_per_h": growth_mb_per_h,
            "leak_suspicion": growth_mb_per_h > 100,
        }

    # ===== memory_components =====
    def memory_components_snapshot(self) -> dict[str, object]:
        """各 runtime store/buffer 内存估算（condition_count + 估算 KB）。"""

        runtime = self._runtime
        if runtime is None:
            return {"available": False}
        components: dict[str, dict[str, object]] = {}
        # orderbook_history_buffer（自带 memory_footprint_estimate）
        if runtime.orderbook_history_buffer is not None:
            try:
                components["orderbook_history_buffer"] = (
                    runtime.orderbook_history_buffer.memory_footprint_estimate()
                )
            except Exception as exc:  # noqa: BLE001
                components["orderbook_history_buffer"] = {"error": str(exc)[:120]}
        # orderbook_delta_store
        if runtime.orderbook_delta_store is not None:
            try:
                ds = runtime.orderbook_delta_store
                tokens = ds.tracked_tokens()
                total_samples = sum(len(ds.samples(t)) for t in tokens)
                components["orderbook_delta_store"] = {
                    "tracked_tokens": len(tokens),
                    "total_samples": total_samples,
                    "estimated_kb": round(total_samples * 0.4, 1),
                }
            except Exception as exc:  # noqa: BLE001
                components["orderbook_delta_store"] = {"error": str(exc)[:120]}
        # market_ws_worker._states
        if runtime.market_ws_worker is not None:
            try:
                ws = runtime.market_ws_worker
                states_count = len(ws._states) if hasattr(ws, "_states") else 0
                components["market_ws_worker_states"] = {
                    "tracked_count": states_count,
                    "estimated_kb": round(states_count * 1.0, 1),
                }
            except Exception as exc:  # noqa: BLE001
                components["market_ws_worker_states"] = {"error": str(exc)[:120]}
        # market_metadata_store
        if runtime.market_metadata_store is not None:
            try:
                records = list(runtime.market_metadata_store.records())
                components["market_metadata_store"] = {
                    "records": len(records),
                    "estimated_kb": round(len(records) * 2.0, 1),
                }
            except Exception as exc:  # noqa: BLE001
                components["market_metadata_store"] = {"error": str(exc)[:120]}
        # account_state_store
        if runtime.account_state_store is not None:
            try:
                acc = runtime.account_state_store.snapshot()
                components["account_state_store"] = {
                    "open_orders": len(acc.open_orders),
                    "fills": len(acc.fills),
                    "positions": len(acc.positions),
                    "market_pauses": len(acc.market_pauses),
                }
            except Exception as exc:  # noqa: BLE001
                components["account_state_store"] = {"error": str(exc)[:120]}
        # paper_ledger
        if runtime.paper_ledger is not None:
            try:
                pl = runtime.paper_ledger
                positions_count = len(pl.positions)
                price_history_samples = sum(
                    len(h) for h in getattr(pl, "position_price_history", {}).values()
                )
                equity_curve_len = len(getattr(pl, "equity_curve", []))
                realized_trades_len = len(getattr(pl, "realized_trades", []))
                components["paper_ledger"] = {
                    "positions": positions_count,
                    "position_price_samples": price_history_samples,
                    "equity_curve_points": equity_curve_len,
                    "realized_trades": realized_trades_len,
                }
            except Exception as exc:  # noqa: BLE001
                components["paper_ledger"] = {"error": str(exc)[:120]}
        # registry
        if runtime.registry is not None:
            try:
                snap = runtime.registry.snapshot()
                components["market_registry"] = {
                    "markets": len(snap.markets),
                    "estimated_kb": round(len(snap.markets) * 4.0, 1),
                }
            except Exception as exc:  # noqa: BLE001
                components["market_registry"] = {"error": str(exc)[:120]}
        # 当前进程 RSS 作总对照
        try:
            import platform
            import resource

            ru = resource.getrusage(resource.RUSAGE_SELF)
            rss_bytes = (
                ru.ru_maxrss if platform.system() == "Darwin" else ru.ru_maxrss * 1024
            )
            total_rss_mb = round(rss_bytes / 1024 / 1024, 1)
        except Exception:  # noqa: BLE001
            total_rss_mb = None
        return {
            "available": True,
            "total_process_rss_mb": total_rss_mb,
            "components": components,
        }

    # ===== memory_objects =====
    def memory_objects_snapshot(self, *, top_n: int = 30) -> dict[str, object]:
        """gc.get_objects() 按 type 分组 top N，识别哪类对象占用最多。"""

        counts: Counter[str] = Counter()
        try:
            for obj in gc.get_objects():
                counts[type(obj).__name__] += 1
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": str(exc)[:200]}
        top = counts.most_common(top_n)
        return {
            "available": True,
            "total_objects": sum(counts.values()),
            "unique_types": len(counts),
            "top_types": [{"type": name, "count": cnt} for name, cnt in top],
        }

    # ===== data_staleness =====
    def data_staleness_snapshot(self) -> dict[str, object]:
        """各数据源的 lag/staleness（supervisor workers + market_ws last_message）。"""

        runtime = self._runtime
        if runtime is None:
            return {"sources": []}
        result: list[dict[str, object]] = []
        try:
            sup = runtime.supervisor
            if sup and hasattr(sup, "workers_snapshot"):
                wsn = sup.workers_snapshot()
                workers = wsn.get("workers", []) if isinstance(wsn, dict) else wsn
                for w in workers:
                    name = w.get("name") if isinstance(w, dict) else None
                    if not name:
                        continue
                    result.append({
                        "source": name,
                        "status": w.get("status") if isinstance(w, dict) else None,
                        "last_heartbeat_at": (
                            w.get("last_heartbeat_at") if isinstance(w, dict) else None
                        ),
                    })
        except Exception:  # noqa: BLE001
            pass
        if runtime.market_ws_worker:
            try:
                ws_st = runtime.market_ws_worker.status_snapshot(include_subscriptions=False)
                now = datetime.now(timezone.utc)
                lag_s = None
                if ws_st.last_message_at:
                    lag_s = round((now - ws_st.last_message_at).total_seconds(), 2)
                result.append({
                    "source": "polymarket_market_ws",
                    "connected": ws_st.connected,
                    "last_message_at": (
                        ws_st.last_message_at.isoformat() if ws_st.last_message_at else None
                    ),
                    "lag_seconds": lag_s,
                    "stale": (lag_s is not None and lag_s > 30),
                })
            except Exception as exc:  # noqa: BLE001
                result.append({"source": "polymarket_market_ws", "error": str(exc)})
        return {
            "sources_count": len(result),
            "healthy_count": sum(1 for r in result if not r.get("stale")),
            "stale_count": sum(1 for r in result if r.get("stale")),
            "sources": result,
        }

    # ===== data_sources_health =====
    def data_sources_health(self) -> dict[str, object]:
        """所有数据源延迟/健康聚合 + 直播/盘口新鲜度桶分布。"""

        runtime = self._runtime
        if runtime is None:
            return {"error": "runtime not bound"}
        now = datetime.now(timezone.utc)
        health: dict[str, object] = {"checked_at": now.isoformat()}

        if runtime.goalserve_lazy_client:
            health["goalserve_lazy"] = runtime.goalserve_lazy_client.cache_status()

        if runtime.market_ws_worker:
            try:
                st = runtime.market_ws_worker.status_snapshot(include_subscriptions=False)
                health["polymarket_market_ws"] = {
                    "tracked_markets": st.tracked_market_count,
                    "subscription_count": st.subscription_count,
                    "connected": st.connected,
                    "last_message_at": (
                        st.last_message_at.isoformat() if st.last_message_at else None
                    ),
                    "last_rest_snapshot_at": (
                        st.last_rest_snapshot_at.isoformat()
                        if st.last_rest_snapshot_at else None
                    ),
                    "last_error": st.last_error,
                    "lag_since_last_message_s": (
                        round((now - st.last_message_at).total_seconds(), 2)
                        if st.last_message_at else None
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                health["polymarket_market_ws"] = {"error": str(exc)}

        if runtime.user_ws_worker:
            try:
                user_ws = runtime.user_ws_worker
                last_at = getattr(user_ws, "_last_message_at", None)
                health["polymarket_user_ws"] = {
                    "connected": getattr(user_ws, "_is_connected", None),
                    "last_message_at": last_at.isoformat() if last_at else None,
                }
            except Exception as exc:  # noqa: BLE001
                health["polymarket_user_ws"] = {"error": str(exc)}

        if runtime.outbox:
            try:
                health["outbox"] = {
                    "pending_count": getattr(runtime.outbox, "pending_count", lambda: None)(),
                }
            except Exception:  # noqa: BLE001
                pass

        try:
            sup = runtime.supervisor
            if sup and hasattr(sup, "workers_snapshot"):
                health["workers_snapshot"] = sup.workers_snapshot()
        except Exception:  # noqa: BLE001
            pass

        try:
            if runtime.sports_live_state_client is not None:
                per_sport = runtime.sports_live_state_client.source_detail_status()
                sport_summary: dict[str, list[dict]] = {}
                for s in per_sport:
                    sp = s.get("sport") or "unknown"
                    sport_summary.setdefault(sp, []).append(s)
                health["per_sport_sources"] = sport_summary
        except Exception as exc:  # noqa: BLE001
            health["per_sport_sources_error"] = str(exc)[:120]

        # live_state 新鲜度桶
        try:
            store = runtime.market_metadata_store
            if store is not None:
                buckets = {
                    "<5s": 0, "5-30s": 0, "30-60s": 0, "60-300s": 0, ">300s": 0, "no_state": 0,
                }
                total = 0
                signal_allowed_count = 0
                ages_ms: list[float] = []
                for rec in store.records():
                    total += 1
                    if not getattr(rec, "live_state_payload", None):
                        buckets["no_state"] += 1
                        continue
                    if rec.live_state_signal_allowed:
                        signal_allowed_count += 1
                    age_s = (now - rec.updated_at).total_seconds()
                    ages_ms.append(age_s * 1000)
                    if age_s < 5:
                        buckets["<5s"] += 1
                    elif age_s < 30:
                        buckets["5-30s"] += 1
                    elif age_s < 60:
                        buckets["30-60s"] += 1
                    elif age_s < 300:
                        buckets["60-300s"] += 1
                    else:
                        buckets[">300s"] += 1
                live_state_summary: dict[str, object] = {
                    "total_markets_with_metadata": total,
                    "signal_allowed_count": signal_allowed_count,
                    "freshness_buckets": buckets,
                }
                if ages_ms:
                    sorted_ages = sorted(ages_ms)
                    n = len(sorted_ages)
                    live_state_summary["age_ms_p50"] = round(sorted_ages[n // 2], 0)
                    live_state_summary["age_ms_p90"] = round(
                        sorted_ages[min(int(n * 0.9), n - 1)], 0
                    )
                    live_state_summary["age_ms_p99"] = round(
                        sorted_ages[min(int(n * 0.99), n - 1)], 0
                    )
                    live_state_summary["age_ms_max"] = round(sorted_ages[-1], 0)
                    live_state_summary["age_ms_avg"] = round(sum(sorted_ages) / n, 0)
                health["live_state_freshness"] = live_state_summary
        except Exception as exc:  # noqa: BLE001
            health["live_state_freshness_error"] = str(exc)[:120]

        # orderbook snapshot 新鲜度桶
        try:
            if runtime.market_ws_worker is not None:
                ws = runtime.market_ws_worker
                ob_buckets = {"<1s": 0, "1-5s": 0, "5-30s": 0, "30-60s": 0, ">60s": 0}
                total_tokens = 0
                ob_ages_ms: list[float] = []
                for _token_id, state in (getattr(ws, "_states", {}) or {}).items():
                    snap = getattr(state, "snapshot", None)
                    if snap is None:
                        continue
                    total_tokens += 1
                    age_s = (now - snap.received_at).total_seconds()
                    ob_ages_ms.append(age_s * 1000)
                    if age_s < 1:
                        ob_buckets["<1s"] += 1
                    elif age_s < 5:
                        ob_buckets["1-5s"] += 1
                    elif age_s < 30:
                        ob_buckets["5-30s"] += 1
                    elif age_s < 60:
                        ob_buckets["30-60s"] += 1
                    else:
                        ob_buckets[">60s"] += 1
                ob_summary: dict[str, object] = {
                    "total_tokens": total_tokens,
                    "freshness_buckets": ob_buckets,
                }
                if ob_ages_ms:
                    sorted_ages = sorted(ob_ages_ms)
                    n = len(sorted_ages)
                    ob_summary["age_ms_p50"] = round(sorted_ages[n // 2], 0)
                    ob_summary["age_ms_p90"] = round(
                        sorted_ages[min(int(n * 0.9), n - 1)], 0
                    )
                    ob_summary["age_ms_max"] = round(sorted_ages[-1], 0)
                health["orderbook_freshness"] = ob_summary
        except Exception as exc:  # noqa: BLE001
            health["orderbook_freshness_error"] = str(exc)[:120]

        # reconcile 状态
        if runtime.account_state_store:
            try:
                acc = runtime.account_state_store.snapshot()
                health["account_state"] = {
                    "last_reconcile_at": (
                        acc.last_reconcile_at.isoformat() if acc.last_reconcile_at else None
                    ),
                    "reconcile_age_seconds": (
                        round((now - acc.last_reconcile_at).total_seconds(), 1)
                        if acc.last_reconcile_at else None
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                health["account_state"] = {"error": str(exc)}

        # trade tape cache 状态（W3c 后会迁到 market_misc_aggregator）
        from polymarket_trader.app.admin_service import _TRADE_TAPE_CACHE

        if _TRADE_TAPE_CACHE:
            ages = [
                round(_time_module.time() - ts, 1) for ts, _ in _TRADE_TAPE_CACHE.values()
            ]
            health["polymarket_trade_tape_cache"] = {
                "entries": len(_TRADE_TAPE_CACHE),
                "min_age_seconds": min(ages) if ages else None,
                "max_age_seconds": max(ages) if ages else None,
            }

        return health

    # ===== live_attention =====
    def live_attention_snapshot(self) -> dict[str, object]:
        """同时 LIVE 比赛数 + per sport 分布（注意力分散度量化指标）。"""

        runtime = self._runtime
        if runtime is None or runtime.market_ws_worker is None:
            return {"tracked_markets": 0, "by_sport": {}}
        ws = runtime.market_ws_worker
        tracked = ws._tracked_markets if hasattr(ws, "_tracked_markets") else {}
        sport_count: Counter[str] = Counter()
        items = tracked.items() if hasattr(tracked, "items") else []
        for _token_id, market in items:
            slug = (
                (market.market_slug or "").lower()
                if hasattr(market, "market_slug") else ""
            )
            sport = "other"
            for s in (
                "kbo", "mlb", "nba", "wnba", "nhl", "nfl", "ncaaf", "ncaab",
                "atp", "wta", "itf", "mls", "epl", "j2100", "j1100",
            ):
                if s in slug:
                    sport = s
                    break
            sport_count[sport] += 1
        tracked_count = len(tracked) if hasattr(tracked, "__len__") else 0
        return {
            "tracked_markets": tracked_count,
            "by_sport": dict(sport_count),
            "unique_sports": len(sport_count),
            "max_sport": sport_count.most_common(1)[0] if sport_count else None,
            "attention_dispersion_score": round(
                len(sport_count) / max(1, tracked_count) * 100, 2
            ),
        }


__all__ = ["SystemObservabilityAggregator"]
