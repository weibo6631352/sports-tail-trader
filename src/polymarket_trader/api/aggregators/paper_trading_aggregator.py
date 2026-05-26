"""PaperTradingAggregator —— paper 模式量化诊断 + 异常检测 + 资金曲线 + CLV。

5 类查询都依赖 `runtime.paper_ledger` + 部分 DB / WS 状态。归在一起是因为
"为什么这单赚/为什么停在那里"的复盘场景需要这 5 个视图配合看：

- `paper_metrics_snapshot()` —— 综合 PnL / 持仓 / 撮合 / sport 集中度
- `paper_orders_snapshot(limit)` —— 最近 N 单 request + simulation 撮合详情
- `equity_curve_snapshot()` —— equity_curve 时序 + drawdown / volatility 派生
- `clv_snapshot()` —— Closing Line Value 漂移（体育博彩黄金 KPI）
- `anomalies_snapshot(window_minutes)` —— reject reason spike / 死仓 / DB 慢警报

# Endpoint 对应

| Endpoint | 方法 |
|---|---|
| `GET /runtime/paper-metrics` | `paper_metrics_snapshot(...)` |
| `GET /runtime/paper-orders` | `paper_orders_snapshot(...)` |
| `GET /runtime/equity-curve` | `equity_curve_snapshot(...)` |
| `GET /runtime/clv` | `clv_snapshot(...)` |
| `GET /runtime/anomalies` | `anomalies_snapshot(...)` |
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean
from typing import Any

logger = logging.getLogger(__name__)


class PaperTradingAggregator:
    def __init__(self, *, runtime: Any) -> None:
        self._runtime = runtime

    # ===== paper_metrics =====
    def paper_metrics_snapshot(self) -> dict[str, object] | None:
        """Paper trading 综合量化指标——PnL/持仓/撮合/sport 集中度 一次拿全。"""

        if self._runtime is None or self._runtime.paper_ledger is None:
            return None
        ledger = self._runtime.paper_ledger
        ws = self._runtime.market_ws_worker
        total_cost = Decimal("0")
        unrealized_value = Decimal("0")
        per_pos: list[dict[str, Any]] = []
        for tok, shares in ledger.positions.items():
            cost = ledger.cost_basis_usdc.get(tok, Decimal("0"))
            total_cost += cost
            best_bid_now: Decimal | None = None
            if ws is not None:
                ob = ws.snapshot(tok)
                if ob is not None and ob.best_bid is not None and ob.sell_actionable:
                    best_bid_now = ob.best_bid
                    unrealized_value += shares * ob.best_bid
            per_pos.append({
                "token_id": tok[:32],
                "shares": str(shares),
                "cost_usdc": str(cost),
                "avg_price": (
                    str((cost / shares).quantize(Decimal("0.0001"))) if shares > 0 else None
                ),
                "best_bid_now": str(best_bid_now) if best_bid_now is not None else None,
                "current_value": (
                    str(shares * best_bid_now) if best_bid_now is not None else "0"
                ),
                "unrealized_pnl": (
                    str(shares * best_bid_now - cost) if best_bid_now is not None else str(-cost)
                ),
            })
        equity = ledger.available_usdc + unrealized_value
        realized_pnl = (
            ledger.available_usdc
            - self._runtime.settings.portfolio_budget_usdc
            + total_cost
        )
        # 撮合：从 paper client 取 simulations 统计
        execution_client = getattr(self._runtime.order_executor, "_client", None)
        sim_stats = {
            "total": 0, "full_fill": 0, "partial_fill": 0, "no_fill": 0,
            "live": 0, "rejected": 0, "other": 0,
        }
        if execution_client is not None and hasattr(execution_client, "simulations"):
            for _, outcome in execution_client.simulations:
                sim_stats["total"] += 1
                status_obj = outcome.response.status
                status = str(getattr(status_obj, "value", status_obj)).lower()
                if "full" in status:
                    sim_stats["full_fill"] += 1
                elif "partial" in status:
                    sim_stats["partial_fill"] += 1
                elif "no_fill" in status:
                    sim_stats["no_fill"] += 1
                elif "live" in status:
                    sim_stats["live"] += 1
                elif "reject" in status:
                    sim_stats["rejected"] += 1
                else:
                    sim_stats["other"] += 1
        ws_status = ws.status_snapshot(include_subscriptions=False) if ws else None
        ws_metrics: dict[str, Any] = {}
        if ws_status is not None:
            ws_metrics = {
                "tracked_markets": ws_status.tracked_market_count,
                "subscription_count": ws_status.subscription_count,
                "connected": ws_status.connected,
            }
        sport_concentration: Counter[str] = Counter()
        for tok in ledger.positions.keys():
            market = (
                self._runtime.registry.get_by_token_id(tok)
                if self._runtime.registry else None
            )
            if market is None:
                continue
            slug = (market.market_slug or "").lower()
            sport = "unknown"
            for s in (
                "mlb", "nba", "wnba", "nhl", "nfl", "ncaaf", "ncaab", "kbo",
                "atp", "wta", "itf", "mls", "epl", "laliga", "j2100", "j1100",
            ):
                if s in slug:
                    sport = s
                    break
            sport_concentration[sport] += 1
        max_sport = sport_concentration.most_common(1)
        max_sport_count = max_sport[0][1] if max_sport else 0
        max_sport_pct = (
            (max_sport_count / len(ledger.positions) * 100) if ledger.positions else 0
        )
        realized = ledger.realized_trades
        avg_holding = None
        max_holding = None
        avg_drawdown_pct = None
        if realized:
            holding_times = [t.get("holding_seconds") for t in realized if t.get("holding_seconds")]
            if holding_times:
                avg_holding = round(sum(holding_times) / len(holding_times), 1)
                max_holding = round(max(holding_times), 1)
            try:
                drawdowns = [
                    Decimal(t.get("max_drawdown_pct", "0"))
                    for t in realized
                    if t.get("max_drawdown_pct")
                ]
                if drawdowns:
                    avg_drawdown_pct = float(sum(drawdowns) / len(drawdowns))
            except Exception:  # noqa: BLE001
                pass
        realized_summary = {
            "trades_count": len(realized),
            "avg_holding_seconds": avg_holding,
            "max_holding_seconds": max_holding,
            "avg_max_drawdown_pct": avg_drawdown_pct,
            "recent_trades": realized[-10:],
        }
        budget = self._runtime.settings.portfolio_budget_usdc
        return {
            "paper_trading_mode": True,
            "initial_budget_usdc": str(budget),
            "realized_trades_summary": realized_summary,
            "concentration": {
                "by_sport": dict(sport_concentration),
                "max_sport": max_sport[0][0] if max_sport else None,
                "max_sport_count": max_sport_count,
                "max_sport_pct": round(max_sport_pct, 1),
                "total_positions": len(ledger.positions),
                "warning_over_concentrated": max_sport_pct > 50,
            },
            "funds": {
                "available_usdc": str(ledger.available_usdc),
                "total_cost_usdc": str(total_cost),
                "unrealized_value_usdc": str(unrealized_value),
                "realized_pnl_usdc": str(realized_pnl.quantize(Decimal("0.01"))),
                "unrealized_pnl_usdc": str((unrealized_value - total_cost).quantize(Decimal("0.01"))),
                "total_pnl_usdc": str((equity - budget).quantize(Decimal("0.01"))),
                "total_equity_usdc": str(equity.quantize(Decimal("0.01"))),
                "fees_accrued_usdc": str(ledger.fees_accrued_usdc),
                "fees_pct_of_initial": (
                    str((ledger.fees_accrued_usdc / budget * 100).quantize(Decimal("0.01"))) + "%"
                ),
            },
            "positions": {"count": len(ledger.positions), "by_token": per_pos},
            "simulations": sim_stats,
            "ws_market": ws_metrics,
            "goal_progress": {
                "initial": "100",
                "target": "1000",
                "current_equity": str(equity.quantize(Decimal("0.01"))),
                "progress_pct": (
                    str(((equity - Decimal("100")) / Decimal("900") * 100).quantize(Decimal("0.01"))) + "%"
                ),
            },
        }

    # ===== paper_orders =====
    def paper_orders_snapshot(self, *, limit: int = 50) -> dict[str, object] | None:
        """Paper trading 最近 N 条订单 + 撮合结果详情。"""

        if self._runtime is None or self._runtime.paper_ledger is None:
            return None
        execution_client = (
            getattr(self._runtime.order_executor, "_client", None)
            if self._runtime.order_executor else None
        )
        if execution_client is None or not hasattr(execution_client, "requests"):
            return {
                "paper_trading_mode": True,
                "requests": [],
                "simulations": [],
                "note": "execution_client unavailable",
            }
        requests_data: list[dict[str, Any]] = []
        for phase, req in list(execution_client.requests)[-limit:]:
            requests_data.append({
                "phase": phase,
                "trace_id": req.trace_id,
                "token_id": (req.token_id or "")[:32],
                "market_slug": req.market_slug,
                "side": req.side.value if req.side else None,
                "order_type": req.order_type.value if req.order_type else None,
                "price": str(req.price) if req.price is not None else None,
                "size_shares": str(req.size_shares) if req.size_shares is not None else None,
                "amount_usdc": str(req.amount_usdc) if req.amount_usdc is not None else None,
                "reason": req.reason,
            })
        simulations_data: list[dict[str, Any]] = []
        for trace_id, outcome in list(execution_client.simulations)[-limit:]:
            resp = outcome.response
            match = outcome.match_result
            fee = outcome.fee_quote
            simulations_data.append({
                "trace_id": trace_id,
                "status": getattr(resp.status, "value", str(resp.status)),
                "reason": resp.reason,
                "matched_shares": (
                    str(resp.matched_shares) if resp.matched_shares is not None else None
                ),
                "spent_usdc": str(resp.spent_usdc) if resp.spent_usdc is not None else None,
                "remaining_shares": (
                    str(resp.remaining_shares) if resp.remaining_shares is not None else None
                ),
                "match": None if match is None else {
                    "filled_shares": str(match.filled_shares),
                    "avg_price": str(match.avg_price) if match.avg_price else None,
                    "consumed_count": len(match.consumed_levels),
                    "unfilled_amount_usdc": str(match.unfilled_amount_usdc),
                    "unfilled_size_shares": str(match.unfilled_size_shares),
                },
                "fee_usdc": str(fee.fee_usdc) if fee else None,
                "fee_rate_bps": fee.fee_rate_bps if fee else None,
            })
        return {
            "paper_trading_mode": True,
            "total_requests": len(execution_client.requests),
            "total_simulations": len(execution_client.simulations),
            "requests": requests_data,
            "simulations": simulations_data,
        }

    # ===== equity_curve =====
    def equity_curve_snapshot(self) -> dict[str, object] | None:
        """资金曲线时序 + 派生 max drawdown / volatility / return。"""

        if self._runtime is None or self._runtime.paper_ledger is None:
            return None
        curve = getattr(self._runtime.paper_ledger, "equity_curve", [])
        if not curve:
            return {"curve": [], "points_count": 0}
        equities = [Decimal(p["equity_usdc"]) for p in curve]
        peak = max(equities)
        current = equities[-1]
        initial = equities[0]
        max_dd_pct = Decimal("0")
        running_peak = equities[0]
        for eq in equities:
            if eq > running_peak:
                running_peak = eq
            if running_peak > 0:
                dd = (running_peak - eq) / running_peak * Decimal("100")
                if dd > max_dd_pct:
                    max_dd_pct = dd
        current_dd_pct = (
            ((peak - current) / peak * Decimal("100")) if peak > 0 else Decimal("0")
        )
        recent = equities[-60:] if len(equities) >= 2 else equities
        if len(recent) >= 2:
            mean_v = sum(recent) / len(recent)
            var = sum((e - mean_v) ** 2 for e in recent) / len(recent)
            std_v = (
                var.sqrt() if hasattr(var, "sqrt")
                else Decimal(str(float(var) ** 0.5))
            )
        else:
            std_v = Decimal("0")
        return {
            "points_count": len(curve),
            "initial_equity": str(initial),
            "current_equity": str(current),
            "peak_equity": str(peak),
            "return_pct": (
                str(((current - initial) / initial * Decimal("100")).quantize(Decimal("0.01")))
                if initial > 0 else "0"
            ),
            "current_drawdown_pct": str(current_dd_pct.quantize(Decimal("0.01"))),
            "max_drawdown_pct": str(max_dd_pct.quantize(Decimal("0.01"))),
            "volatility_60min_usdc": str(std_v.quantize(Decimal("0.01"))),
            "first_point_at": curve[0].get("at"),
            "last_point_at": curve[-1].get("at"),
            "curve": curve[-60:],
        }

    # ===== clv =====
    def clv_snapshot(self) -> dict[str, object] | None:
        """CLV (Closing Line Value) 跟踪 — 入场后价格漂移分析。"""

        if self._runtime is None or self._runtime.paper_ledger is None:
            return None
        ledger = self._runtime.paper_ledger
        ws = self._runtime.market_ws_worker

        def _clv_at(
            history: list, entry_ts: datetime, target_seconds: int, entry_price: Decimal,
        ) -> dict | None:
            if not history or not entry_ts:
                return None
            target_at = entry_ts + timedelta(seconds=target_seconds)
            best_sample = None
            for ts_str, bid_str in history:
                try:
                    ts = datetime.fromisoformat(ts_str)
                except Exception:  # noqa: BLE001
                    continue
                if ts >= target_at:
                    best_sample = (ts, Decimal(str(bid_str)))
                    break
            if best_sample is None and history:
                ts_str, bid_str = history[-1]
                try:
                    best_sample = (datetime.fromisoformat(ts_str), Decimal(str(bid_str)))
                except Exception:  # noqa: BLE001
                    return None
            if best_sample is None:
                return None
            ts, bid = best_sample
            clv = bid - entry_price
            clv_pct = (
                (clv / entry_price * Decimal("100")).quantize(Decimal("0.01"))
                if entry_price > 0 else Decimal("0")
            )
            return {
                "sample_at": ts.isoformat(),
                "best_bid": str(bid),
                "clv_usdc": str(clv.quantize(Decimal("0.0001"))),
                "clv_pct": str(clv_pct),
            }

        open_clv: list[dict] = []
        for token_id, shares in ledger.positions.items():
            if shares <= Decimal("0"):
                continue
            cost = ledger.cost_basis_usdc.get(token_id, Decimal("0"))
            entry_price = cost / shares if shares > 0 else Decimal("0")
            entry_at = ledger.first_fill_at.get(token_id)
            history = ledger.position_price_history.get(token_id, [])
            market = (
                self._runtime.registry.get_by_token_id(token_id)
                if self._runtime.registry else None
            )
            slug = market.market_slug if market else None
            now_bid: Decimal | None = None
            if ws:
                ob = ws.snapshot(token_id)
                if ob and ob.best_bid is not None:
                    now_bid = ob.best_bid
            entry: dict = {
                "token_id": token_id[:32],
                "market_slug": slug,
                "shares": str(shares),
                "entry_price": str(entry_price.quantize(Decimal("0.0001"))),
                "entry_at": entry_at.isoformat() if entry_at else None,
                "holding_seconds": (
                    (datetime.utcnow().replace(tzinfo=entry_at.tzinfo) - entry_at).total_seconds()
                    if entry_at else None
                ),
                "price_samples": len(history),
                "best_bid_now": str(now_bid) if now_bid else None,
                "current_clv_usdc": (
                    str((now_bid - entry_price).quantize(Decimal("0.0001")))
                    if now_bid is not None else None
                ),
            }
            if entry_at:
                for label, seconds in (("1m", 60), ("5m", 300), ("15m", 900), ("30m", 1800)):
                    entry[f"clv_{label}"] = _clv_at(history, entry_at, seconds, entry_price)
            open_clv.append(entry)

        closed_clv: list[dict] = []
        for trade in ledger.realized_trades[-50:]:
            history = trade.get("price_history", [])
            closed_clv.append({
                "token_id": trade.get("token_id", "")[:32],
                "entry_at": trade.get("entry_at"),
                "exit_at": trade.get("exit_at"),
                "holding_seconds": trade.get("holding_seconds"),
                "realized_pnl": trade.get("realized_pnl"),
                "max_drawdown_pct": trade.get("max_drawdown_pct"),
                "price_samples": len(history),
                "price_first": history[0][1] if history else None,
                "price_last": history[-1][1] if history else None,
                "price_min": (
                    min((float(b) for _, b in history), default=None) if history else None
                ),
                "price_max": (
                    max((float(b) for _, b in history), default=None) if history else None
                ),
            })

        avg_current_clv = None
        if open_clv:
            clvs = [float(p["current_clv_usdc"]) for p in open_clv if p.get("current_clv_usdc")]
            if clvs:
                avg_current_clv = round(mean(clvs), 4)
        return {
            "open_positions_clv": open_clv,
            "closed_trades_clv": closed_clv,
            "summary": {
                "open_count": len(open_clv),
                "closed_count": len(closed_clv),
                "avg_current_clv_usdc": avg_current_clv,
            },
        }

    # ===== anomalies =====
    async def anomalies_snapshot(self, *, window_minutes: int = 5) -> dict[str, object]:
        """异常检测（reject reason 突变 / 死仓 / 集中度告警 / DB 慢警报 / outbox 积压）。"""

        anomalies: list[dict] = []
        if self._runtime is None or self._runtime.db_session_factory is None:
            return {"anomalies": anomalies, "checked_at": None}
        from sqlalchemy import text as sql_text

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        cutoff_prev = datetime.now(timezone.utc) - timedelta(minutes=window_minutes * 2)
        try:
            async with self._runtime.db_session_factory() as session:
                result = await session.execute(
                    sql_text(
                        """
                        SELECT COALESCE(payload->>'reason', reason, '?') as r, COUNT(*) as n
                        FROM audit_events
                        WHERE created_at > :cutoff
                          AND event_title IN ('order_rejected','risk_rejection_recorded')
                        GROUP BY 1
                        """
                    ),
                    {"cutoff": cutoff},
                )
                current = {r[0]: r[1] for r in result}
                result2 = await session.execute(
                    sql_text(
                        """
                        SELECT COALESCE(payload->>'reason', reason, '?') as r, COUNT(*) as n
                        FROM audit_events
                        WHERE created_at > :cutoff_prev AND created_at <= :cutoff
                          AND event_title IN ('order_rejected','risk_rejection_recorded')
                        GROUP BY 1
                        """
                    ),
                    {"cutoff_prev": cutoff_prev, "cutoff": cutoff},
                )
                previous = {r[0]: r[1] for r in result2}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "anomalies": []}
        # reject reason 突变检测
        for reason, count in current.items():
            prev_count = previous.get(reason, 0)
            if count >= 5 and (prev_count == 0 or count >= prev_count * 3):
                anomalies.append({
                    "type": "reject_reason_spike",
                    "severity": "high" if count >= prev_count * 5 else "medium",
                    "reason": reason,
                    "current_count": count,
                    "previous_count": prev_count,
                    "spike_factor": round(count / (prev_count + 0.1), 1),
                })
        # 死仓识别
        if self._runtime.paper_ledger:
            ws = self._runtime.market_ws_worker
            now = datetime.now(timezone.utc)
            for tok, shares in self._runtime.paper_ledger.positions.items():
                entry_at = self._runtime.paper_ledger.first_fill_at.get(tok)
                if entry_at:
                    age_h = (now - entry_at).total_seconds() / 3600
                    if age_h > 6:
                        ob = ws.snapshot(tok) if ws else None
                        if ob is None or ob.best_bid is None:
                            anomalies.append({
                                "type": "dead_position",
                                "severity": "medium",
                                "token_id": tok[:32],
                                "age_hours": round(age_h, 1),
                                "shares": str(shares),
                                "reason": "best_bid=None for 6h+",
                            })
        # sport 集中度
        if self._runtime.paper_ledger and self._runtime.registry:
            sc: Counter[str] = Counter()
            for tok in self._runtime.paper_ledger.positions:
                m = self._runtime.registry.get_by_token_id(tok)
                if m:
                    slug = (m.market_slug or "").lower()
                    sport = "other"
                    for s in ("kbo", "mlb", "nba", "wnba", "nhl", "atp", "wta", "itf", "mls", "j2100", "j1100"):
                        if s in slug:
                            sport = s
                            break
                    sc[sport] += 1
            if sc:
                top, count = sc.most_common(1)[0]
                total = sum(sc.values())
                pct = count / total * 100
                if pct > 60:
                    anomalies.append({
                        "type": "concentration_warning",
                        "severity": "medium",
                        "sport": top,
                        "positions": count,
                        "total_positions": total,
                        "concentration_pct": round(pct, 1),
                    })
        # DB / outbox 告警
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor

            perf = SystemPerfMonitor.get().snapshot()
            db_q = perf.get("db_queries", {})
            db_p99 = db_q.get("p99")
            db_max = db_q.get("max")
            if isinstance(db_p99, (int, float)):
                if db_p99 >= 1000:
                    anomalies.append({
                        "type": "db_query_p99_critical", "severity": "critical",
                        "p99_ms": db_p99, "max_ms": db_max,
                        "reason": "DB query p99 ≥1s — P0 链路不受影响(异步)但持久化/operator 严重滞后",
                    })
                elif db_p99 >= 500:
                    anomalies.append({
                        "type": "db_query_p99_high", "severity": "high",
                        "p99_ms": db_p99, "max_ms": db_max,
                        "reason": "DB query p99 ≥500ms — outbox/audit 写入滞后",
                    })
                elif db_p99 >= 200:
                    anomalies.append({
                        "type": "db_query_p99_warn", "severity": "medium",
                        "p99_ms": db_p99,
                        "reason": "DB query p99 ≥200ms — 抖动可能开始影响审计可见延迟",
                    })
            for row in db_q.get("top_slow_by_p99", [])[:5]:
                p99 = row.get("p99_ms") or 0
                if p99 >= 800:
                    sev = "high" if p99 >= 1500 else "medium"
                    anomalies.append({
                        "type": "db_sql_slow", "severity": sev,
                        "sql_key": row.get("sql_key"), "p99_ms": p99,
                        "count": row.get("count"), "max_ms": row.get("max_ms"),
                        "reason": "单条 SQL p99 ≥800ms — 排查索引/锁/全表扫",
                    })
            for row in db_q.get("top_slow_by_p99", []):
                key = (row.get("sql_key") or "").upper()
                p99 = row.get("p99_ms") or 0
                if key.startswith("INSERT ") and p99 >= 300:
                    anomalies.append({
                        "type": "db_write_slow", "severity": "medium",
                        "sql_key": row.get("sql_key"), "p99_ms": p99,
                        "count": row.get("count"),
                        "reason": "INSERT 写入 p99 ≥300ms — checkpoint/WAL/lock 抖动",
                    })
            ready: int | None = None
            if (
                self._runtime
                and self._runtime.outbox
                and hasattr(self._runtime.outbox, "snapshot")
            ):
                try:
                    ready, _retained, _dead = self._runtime.outbox.snapshot()
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(ready, int) and ready >= 1000:
                anomalies.append({
                    "type": "outbox_backlog", "severity": "high",
                    "ready_count": ready,
                    "reason": "outbox ready_count ≥1000 — persistence worker 跟不上",
                })
            elif isinstance(ready, int) and ready >= 500:
                anomalies.append({
                    "type": "outbox_backlog", "severity": "medium",
                    "ready_count": ready,
                    "reason": "outbox ready_count ≥500 — 持久化滞后",
                })
        except Exception as exc:  # noqa: BLE001
            anomalies.append({
                "type": "db_alert_check_failed",
                "severity": "low",
                "reason": str(exc)[:200],
            })
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "window_minutes": window_minutes,
            "anomalies_count": len(anomalies),
            "anomalies": anomalies,
        }


__all__ = ["PaperTradingAggregator"]
