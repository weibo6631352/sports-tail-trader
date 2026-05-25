from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents


from polymarket_trader.app.admin_order_control import AdminOrderController
from polymarket_trader.app.admin_runtime_view import AdminRuntimeView
from polymarket_trader.app.admin_serialization import AdminSerializer, decimal_text, jsonable
from polymarket_trader.app.admin_service_helpers import (
    _RepositoryGroup,
)
from polymarket_trader.app.order_projection import AccountStateProjector, normalize_order_id
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.contracts.manual_confirmation import ManualConfirmation
from polymarket_trader.app.decision_serialization import (
    TRADING_DECISION_WORKER_ORIGIN,
    serialize_allocation,
    serialize_allocation_plan,
    serialize_intent,
    serialize_plan_metadata,
    serialize_review,
)
from polymarket_trader.infra.db import (
    AllocationRepository,
    AuditEventRepository,
    DecisionRecordRepository,
    FillRepository,
    MarketRepository,
    OrderRepository,
    OrderbookSnapshotRepository,
    OutboxEventRepository,
    PositionRepository,
    RepositoryPage,
)
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.app.admin_controls_mixin import AdminControlsMixin
from polymarket_trader.app.admin_query import AdminQueryMixin


# Module-level cache for trade tape（frozen dataclass 不能含 mutable state）
_TRADE_TAPE_CACHE: dict[str, tuple[float, dict]] = {}


@dataclass(frozen=True, slots=True)
class AdminService(AdminQueryMixin, AdminControlsMixin):
    """Coordinates read-only admin queries and controlled manual operations.

    Read methods 来自 ``AdminQueryMixin``；受控操作来自 ``AdminControlsMixin``；
    私有 helper（_serializer / _runtime_view / _resolve_market / ...）保留在本类内。
    """

    runtime: RuntimeComponents | None = None

    def bind_runtime(self, runtime: RuntimeComponents) -> "AdminService":
        return AdminService(runtime=runtime)

    def risk_metrics_snapshot(self) -> dict[str, object] | None:
        """量化风险度量（Sharpe / Sortino / Calmar / VaR）。

        基于 equity_curve 时序计算（每分钟 1 点）：
        - Sharpe = mean(returns) / std(returns) × sqrt(periods_per_year)
        - Sortino = mean(returns) / downside_std × sqrt(periods_per_year)
        - Calmar = annualized_return / max_drawdown
        - VaR 95/99 = 历史 returns 5%/1% 分位损失
        """
        if self.runtime is None or self.runtime.paper_ledger is None:
            return None
        curve = getattr(self.runtime.paper_ledger, "equity_curve", [])
        if len(curve) < 5:
            return {"error": "需要至少 5 个数据点（每分钟 1 点）", "points_count": len(curve)}
        equities = [float(p["equity_usdc"]) for p in curve]
        # period returns（per minute）
        returns = [(equities[i] - equities[i-1]) / equities[i-1] for i in range(1, len(equities)) if equities[i-1] > 0]
        if not returns:
            return {"error": "no valid returns", "points_count": len(curve)}
        from statistics import mean, stdev
        mean_ret = mean(returns)
        std_ret = stdev(returns) if len(returns) >= 2 else 0
        # Sharpe (annualized: 60 min/h × 24 × 365 = 525600 per year)
        periods_per_year = 525600
        sharpe = (mean_ret / std_ret * (periods_per_year ** 0.5)) if std_ret > 0 else None
        # Sortino: 只算 downside std
        downside = [r for r in returns if r < 0]
        downside_std = stdev(downside) if len(downside) >= 2 else 0
        sortino = (mean_ret / downside_std * (periods_per_year ** 0.5)) if downside_std > 0 else None
        # max drawdown
        peak = equities[0]
        max_dd = 0.0
        for eq in equities:
            if eq > peak: peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd: max_dd = dd
        # Calmar
        total_return = (equities[-1] - equities[0]) / equities[0] if equities[0] > 0 else 0
        # annualized return（按已运行时间外推）
        period_minutes = len(curve)
        annualized_return = total_return * (periods_per_year / period_minutes) if period_minutes > 0 else 0
        calmar = (annualized_return / max_dd) if max_dd > 0 else None
        # VaR 95/99（per-minute losses 分位）
        sorted_returns = sorted(returns)
        var_95 = sorted_returns[int(len(sorted_returns) * 0.05)] if len(sorted_returns) >= 20 else None
        var_99 = sorted_returns[int(len(sorted_returns) * 0.01)] if len(sorted_returns) >= 100 else None
        return {
            "points_count": len(curve),
            "returns_count": len(returns),
            "current_equity": equities[-1],
            "initial_equity": equities[0],
            "total_return_pct": round(total_return * 100, 2),
            "annualized_return_pct": round(annualized_return * 100, 2),
            "mean_return_per_min": round(mean_ret * 100, 4),
            "std_return_per_min": round(std_ret * 100, 4) if std_ret else 0,
            "sharpe_ratio": round(sharpe, 3) if sharpe else None,
            "sortino_ratio": round(sortino, 3) if sortino else None,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "calmar_ratio": round(calmar, 3) if calmar else None,
            "var_95_per_min_pct": round(var_95 * 100, 4) if var_95 else None,
            "var_99_per_min_pct": round(var_99 * 100, 4) if var_99 else None,
            "downside_returns_count": len(downside),
            "downside_pct_of_total": round(len(downside) / len(returns) * 100, 1),
        }

    async def anomalies_snapshot(self, window_minutes: int = 5) -> dict[str, object]:
        """异常检测（reject reason 突变 / 死仓识别 / 集中度告警）。

        Aggregates:
        - reject_reason_spikes：N min 内某 reason 触发数 > prev 窗口的 3× → 告警
        - dead_positions：holding 时长 > 6h 且 best_bid=None 的仓位
        - concentration_warning：单 sport > 50% 仓位
        - error_rate_spike：errors_5min > 10
        """
        anomalies: list[dict] = []
        if self.runtime is None or self.runtime.db_session_factory is None:
            return {"anomalies": anomalies, "checked_at": None}
        from sqlalchemy import text as sql_text
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        cutoff_prev = datetime.now(timezone.utc) - timedelta(minutes=window_minutes * 2)
        try:
            async with self.runtime.db_session_factory() as session:
                # reject reason 当前窗口 vs 前一窗口对比
                result = await session.execute(
                    sql_text("""
                        SELECT COALESCE(payload->>'reason', reason, '?') as r, COUNT(*) as n
                        FROM audit_events
                        WHERE created_at > :cutoff
                          AND event_title IN ('order_rejected','risk_rejection_recorded')
                        GROUP BY 1
                    """),
                    {"cutoff": cutoff},
                )
                current = {r[0]: r[1] for r in result}
                result2 = await session.execute(
                    sql_text("""
                        SELECT COALESCE(payload->>'reason', reason, '?') as r, COUNT(*) as n
                        FROM audit_events
                        WHERE created_at > :cutoff_prev AND created_at <= :cutoff
                          AND event_title IN ('order_rejected','risk_rejection_recorded')
                        GROUP BY 1
                    """),
                    {"cutoff_prev": cutoff_prev, "cutoff": cutoff},
                )
                previous = {r[0]: r[1] for r in result2}
        except Exception as exc:
            return {"error": str(exc), "anomalies": []}
        # 突变检测：3× spike
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
        # 死仓识别（paper 模式）
        if self.runtime.paper_ledger:
            ws = self.runtime.market_ws_worker
            now = datetime.now(timezone.utc)
            for tok, shares in self.runtime.paper_ledger.positions.items():
                entry_at = self.runtime.paper_ledger.first_fill_at.get(tok)
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
        # 集中度告警
        if self.runtime.paper_ledger and self.runtime.registry:
            from collections import Counter
            sc: Counter = Counter()
            for tok in self.runtime.paper_ledger.positions:
                m = self.runtime.registry.get_by_token_id(tok)
                if m:
                    slug = (m.market_slug or "").lower()
                    sport = "other"
                    for s in ("kbo", "mlb", "nba", "wnba", "nhl", "atp", "wta", "itf", "mls", "j2100", "j1100"):
                        if s in slug:
                            sport = s; break
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
        # DB 慢警报:从 SystemPerfMonitor 拿全局 p99 + top slow SQL,触阈值即告
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            perf = SystemPerfMonitor.get().snapshot()
            db_q = perf.get("db_queries", {})
            db_p99 = db_q.get("p99")
            db_max = db_q.get("max")
            # 全局 p99 阈值:200ms warn / 500ms high / 1000ms critical
            if isinstance(db_p99, (int, float)):
                if db_p99 >= 1000:
                    anomalies.append({
                        "type": "db_query_p99_critical", "severity": "critical",
                        "p99_ms": db_p99, "max_ms": db_max,
                        "reason": "DB query p99 ≥1s — P0 链路不受影响(异步)但持久化/admin 严重滞后",
                    })
                elif db_p99 >= 500:
                    anomalies.append({
                        "type": "db_query_p99_high", "severity": "high",
                        "p99_ms": db_p99, "max_ms": db_max,
                        "reason": "DB query p99 ≥500ms — outbox/audit 写入滞后,排查 PG load/disk/checkpoint",
                    })
                elif db_p99 >= 200:
                    anomalies.append({
                        "type": "db_query_p99_warn", "severity": "medium",
                        "p99_ms": db_p99,
                        "reason": "DB query p99 ≥200ms — 抖动可能开始影响审计可见延迟",
                    })
            # 单条 SQL 阈值:某 SQL p99 > 800ms 单独告警
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
            # 写入慢告警(outbox/audit/markets):INSERT p99 > 300ms 持久化滞后
            for row in db_q.get("top_slow_by_p99", []):
                key = (row.get("sql_key") or "").upper()
                p99 = row.get("p99_ms") or 0
                if key.startswith("INSERT ") and p99 >= 300:
                    anomalies.append({
                        "type": "db_write_slow", "severity": "medium",
                        "sql_key": row.get("sql_key"), "p99_ms": p99,
                        "count": row.get("count"),
                        "reason": "INSERT 写入 p99 ≥300ms — checkpoint/WAL/lock 抖动,排查 PG IO",
                    })
            # outbox 积压告警:ready_count > 500 持久化跟不上(直接从 runtime 拿,perf snapshot 不含 outbox)
            ready = None
            if self.runtime and self.runtime.outbox and hasattr(self.runtime.outbox, "snapshot"):
                try:
                    ready, _retained, _dead = self.runtime.outbox.snapshot()
                except Exception: pass
            if isinstance(ready, int) and ready >= 1000:
                anomalies.append({
                    "type": "outbox_backlog", "severity": "high",
                    "ready_count": ready,
                    "reason": "outbox ready_count ≥1000 — persistence worker 跟不上事件 publish,排查 DB 写入或 worker 停止",
                })
            elif isinstance(ready, int) and ready >= 500:
                anomalies.append({
                    "type": "outbox_backlog", "severity": "medium",
                    "ready_count": ready,
                    "reason": "outbox ready_count ≥500 — 持久化滞后,继续观察是否上涨",
                })
        except Exception as exc:
            anomalies.append({"type": "db_alert_check_failed", "severity": "low", "reason": str(exc)[:200]})
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "window_minutes": window_minutes,
            "anomalies_count": len(anomalies),
            "anomalies": anomalies,
        }

    def equity_curve_snapshot(self) -> dict[str, object] | None:
        """资金曲线时序 + 派生 max drawdown / volatility / return。"""
        if self.runtime is None or self.runtime.paper_ledger is None:
            return None
        from decimal import Decimal
        curve = getattr(self.runtime.paper_ledger, "equity_curve", [])
        if not curve:
            return {"curve": [], "points_count": 0}
        equities = [Decimal(p["equity_usdc"]) for p in curve]
        peak = max(equities)
        current = equities[-1]
        initial = equities[0]
        # max drawdown：滚动 peak 到 trough
        max_dd_pct = Decimal("0")
        running_peak = equities[0]
        for eq in equities:
            if eq > running_peak: running_peak = eq
            if running_peak > 0:
                dd = (running_peak - eq) / running_peak * Decimal("100")
                if dd > max_dd_pct: max_dd_pct = dd
        current_dd_pct = ((peak - current) / peak * Decimal("100")) if peak > 0 else Decimal("0")
        # 最近 60 点 stdev
        recent = equities[-60:] if len(equities) >= 2 else equities
        if len(recent) >= 2:
            mean_v = sum(recent) / len(recent)
            var = sum((e - mean_v) ** 2 for e in recent) / len(recent)
            std_v = var.sqrt() if hasattr(var, "sqrt") else Decimal(str(float(var) ** 0.5))
        else:
            std_v = Decimal("0")
        return {
            "points_count": len(curve),
            "initial_equity": str(initial),
            "current_equity": str(current),
            "peak_equity": str(peak),
            "return_pct": str(((current - initial) / initial * Decimal("100")).quantize(Decimal("0.01"))) if initial > 0 else "0",
            "current_drawdown_pct": str(current_dd_pct.quantize(Decimal("0.01"))),
            "max_drawdown_pct": str(max_dd_pct.quantize(Decimal("0.01"))),
            "volatility_60min_usdc": str(std_v.quantize(Decimal("0.01"))),
            "first_point_at": curve[0].get("at"),
            "last_point_at": curve[-1].get("at"),
            "curve": curve[-60:],  # 默认返回最近 60 点（1h）
        }


    async def market_trade_tape(self, condition_id: str, limit: int = 50) -> dict[str, object]:
        """Polymarket 公开 trade tape — 该 market 最近 N 笔实际成交。

        含每笔 side(BUY/SELL) / size / price / timestamp / wallet(proxyWallet)，
        是 OFI 之外**真正订单流**的来源（OFI 只算盘口变化，trade tape 是实成交）。
        派生:
        - buy_volume / sell_volume：方向流量
        - large_trades：> $100 notional 的大单
        - whale_wallets：高频成交地址
        - avg_trade_size / latest_trade_at
        """
        import time
        import httpx
        cache_key = f"{condition_id}:{limit}"
        cached = _TRADE_TAPE_CACHE.get(cache_key)
        # 2s cache 防止短时间反复 API 调用，仍接近实时（polymarket data-api 无明示限速）
        if cached and (time.time() - cached[0]) < 2:
            return cached[1]
        url = f"https://data-api.polymarket.com/trades?market={condition_id}&limit={limit}"
        import os
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        try:
            mounts = {"https://": httpx.AsyncHTTPTransport(proxy=proxy)} if proxy else None
            async with httpx.AsyncClient(mounts=mounts, trust_env=False, timeout=8) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    return {"error": f"http {r.status_code}", "trades": []}
                data = r.json()
                trades = data if isinstance(data, list) else (data.get("data") or [])
        except Exception as exc:
            return {"error": str(exc), "trades": []}
        # 派生指标
        from collections import Counter
        from decimal import Decimal
        buys = sells = 0
        buy_notional = sell_notional = Decimal("0")
        wallets = Counter()
        large_trades = []
        latest_ts = 0
        normalized = []
        for t in trades:
            if not isinstance(t, dict): continue
            side = (t.get("side") or "").upper()
            try:
                size = Decimal(str(t.get("size", 0)))
                price = Decimal(str(t.get("price", 0)))
            except Exception:
                continue
            notional = size * price
            ts = int(t.get("timestamp", 0))
            latest_ts = max(latest_ts, ts)
            wallet = (t.get("proxyWallet") or "")[:10]
            wallets[wallet] += 1
            if side == "BUY":
                buys += 1
                buy_notional += notional
            elif side == "SELL":
                sells += 1
                sell_notional += notional
            entry = {
                "side": side,
                "size": str(size),
                "price": str(price),
                "notional_usdc": str(notional.quantize(Decimal("0.01"))),
                "timestamp": ts,
                "wallet": wallet,
                "outcome": t.get("outcome"),
                "slug": t.get("slug"),
            }
            if notional >= Decimal("100"):
                large_trades.append(entry)
            normalized.append(entry)
        total_count = buys + sells
        result = {
            "condition_id": condition_id,
            "trades_count": total_count,
            "buy_count": buys,
            "sell_count": sells,
            "buy_notional_usdc": str(buy_notional.quantize(Decimal("0.01"))),
            "sell_notional_usdc": str(sell_notional.quantize(Decimal("0.01"))),
            "net_flow_usdc": str((buy_notional - sell_notional).quantize(Decimal("0.01"))),
            "buy_flow_pct": round(float(buy_notional / (buy_notional + sell_notional) * 100), 1) if (buy_notional + sell_notional) > 0 else None,
            "avg_trade_size_usdc": str(((buy_notional + sell_notional) / total_count).quantize(Decimal("0.01"))) if total_count > 0 else "0",
            "large_trades_count": len(large_trades),
            "unique_wallets": len(wallets),
            "top_wallets": dict(wallets.most_common(5)),
            "latest_trade_at": latest_ts,
            "trades": normalized[:20],
            "large_trades": large_trades[:10],
        }
        _TRADE_TAPE_CACHE[cache_key] = (time.time(), result)
        if len(_TRADE_TAPE_CACHE) > 200:
            _TRADE_TAPE_CACHE.clear()
        return result

    def clv_snapshot(self) -> dict[str, object] | None:
        """CLV (Closing Line Value) 跟踪 — 入场后价格漂移分析。

        体育博彩黄金 KPI：CLV > 0 = 入场价低于后续市场价 = "抢到先手"。
        长期 avg CLV > fees = 策略真正 +EV。

        数据源:
        - open positions: position_price_history（30s 采样的 best_bid 时序）
        - closed positions: realized_trades.price_history（已嵌入完整时序）

        per position 计算:
        - entry_price = cost_basis / shares
        - 1min/5min/15min/30min 后的 best_bid 采样
        - CLV_t = best_bid_t - entry_price（按 t 时长分段）
        - CLV_pct = CLV / entry_price
        """
        if self.runtime is None or self.runtime.paper_ledger is None:
            return None
        from decimal import Decimal
        from datetime import datetime, timedelta
        ledger = self.runtime.paper_ledger
        ws = self.runtime.market_ws_worker

        def _clv_at(history: list, entry_ts: datetime, target_seconds: int, entry_price: Decimal) -> dict | None:
            """从 (ts, bid) 时序中取入场后 target_seconds 秒最近的样本算 CLV。"""
            if not history or not entry_ts:
                return None
            target_at = entry_ts + timedelta(seconds=target_seconds)
            best_sample = None
            for ts_str, bid_str in history:
                try:
                    ts = datetime.fromisoformat(ts_str)
                except Exception:
                    continue
                if ts >= target_at:
                    best_sample = (ts, Decimal(str(bid_str)))
                    break
            if best_sample is None and history:
                # 还没到目标时间 → 取最新一条
                ts_str, bid_str = history[-1]
                try:
                    best_sample = (datetime.fromisoformat(ts_str), Decimal(str(bid_str)))
                except Exception:
                    return None
            if best_sample is None:
                return None
            ts, bid = best_sample
            clv = bid - entry_price
            clv_pct = (clv / entry_price * Decimal("100")).quantize(Decimal("0.01")) if entry_price > 0 else Decimal("0")
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
            market = self.runtime.registry.get_by_token_id(token_id) if self.runtime.registry else None
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
                "holding_seconds": (datetime.utcnow().replace(tzinfo=entry_at.tzinfo) - entry_at).total_seconds() if entry_at else None,
                "price_samples": len(history),
                "best_bid_now": str(now_bid) if now_bid else None,
                "current_clv_usdc": str((now_bid - entry_price).quantize(Decimal("0.0001"))) if now_bid is not None else None,
            }
            # 1/5/15/30 分钟 CLV
            if entry_at:
                for label, seconds in (("1m", 60), ("5m", 300), ("15m", 900), ("30m", 1800)):
                    entry[f"clv_{label}"] = _clv_at(history, entry_at, seconds, entry_price)
            open_clv.append(entry)

        # 已平仓 trades 的 CLV
        closed_clv: list[dict] = []
        for trade in ledger.realized_trades[-50:]:
            entry_at_str = trade.get("entry_at")
            cost = Decimal(str(trade.get("cost", "0")))
            shares = Decimal("0")
            history = trade.get("price_history", [])
            # 从 cost / received 推算 entry/exit price 大致（trade 没存 shares 字段）
            # 用 received_net / pnl 反推 shares 困难，直接用 history 看趋势
            closed_clv.append({
                "token_id": trade.get("token_id", "")[:32],
                "entry_at": entry_at_str,
                "exit_at": trade.get("exit_at"),
                "holding_seconds": trade.get("holding_seconds"),
                "realized_pnl": trade.get("realized_pnl"),
                "max_drawdown_pct": trade.get("max_drawdown_pct"),
                "price_samples": len(history),
                "price_first": history[0][1] if history else None,
                "price_last": history[-1][1] if history else None,
                "price_min": min((float(b) for _, b in history), default=None) if history else None,
                "price_max": max((float(b) for _, b in history), default=None) if history else None,
            })

        # 聚合：avg CLV 现状
        from statistics import mean
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

    async def h2h_snapshot(self, team1_id: str, team2_id: str) -> dict[str, object] | None:
        if self.runtime is None or self.runtime.goalserve_lazy_client is None:
            return None
        return await self.runtime.goalserve_lazy_client.h2h(team1_id, team2_id)

    def derived_metrics_snapshot(self, market_slug: str | None = None) -> dict[str, object]:
        """派生量化指标：基于已有时序数据计算高阶统计/特征。

        对每个 market（或全部活跃 markets）输出：
        - odds_volatility: goalserve 赔率最近 1/5min std（隐含概率波动）
        - odds_drift_rate: 漂移速率（最近 30s/1min 变化）
        - market_efficiency_score: vig 稳定性（vig std 越低越高效）
        - microstructure_pressure: imbalance × momentum × flow 综合压力
        - position_quality_score: per 持仓 holding + drawdown + price_trend 综合
        - signal_consensus: orderbook_direction + odds_drift + game_progress 一致性
        """
        from polymarket_trader.workers.trading_decision.worker import get_odds_drift_store
        from decimal import Decimal
        from statistics import mean, stdev
        store = get_odds_drift_store()
        if not store:
            return {"markets": [], "summary": {"tracked": 0}}

        results: list[dict] = []
        for slug, dq in store.items():
            if market_slug and slug != market_slug:
                continue
            samples = list(dq)
            if len(samples) < 3:
                continue
            # 1. odds volatility (最近 60 个采样 ~ 2min)
            recent = samples[-60:]
            home_probs = [float(s["ml_home_p"]) for s in recent if s.get("ml_home_p") is not None]
            away_probs = [float(s["ml_away_p"]) for s in recent if s.get("ml_away_p") is not None]
            home_std = round(stdev(home_probs), 4) if len(home_probs) >= 2 else 0
            away_std = round(stdev(away_probs), 4) if len(away_probs) >= 2 else 0
            # 2. drift rate (最近 30s ~ 15 个采样 first vs last)
            short_window = samples[-15:] if len(samples) >= 15 else samples
            ml_home_drift = None
            if len(short_window) >= 2 and short_window[0].get("ml_home_p") and short_window[-1].get("ml_home_p"):
                ml_home_drift = round(float(short_window[-1]["ml_home_p"]) - float(short_window[0]["ml_home_p"]), 4)
            # 3. market efficiency (vig stability)
            vigs = []
            for s in recent:
                hp, ap = s.get("ml_home_p"), s.get("ml_away_p")
                if hp is not None and ap is not None:
                    vigs.append(float(hp) + float(ap) - 1.0)
            vig_mean = round(mean(vigs), 4) if vigs else None
            vig_std = round(stdev(vigs), 4) if len(vigs) >= 2 else None
            efficiency = None
            if vig_std is not None and vig_std > 0:
                efficiency = round(1.0 / (1.0 + vig_std * 10), 3)  # 0-1 越高越高效
            # 4. trend label
            trend = "stable"
            if ml_home_drift is not None:
                if ml_home_drift > 0.02: trend = "home_strengthening"
                elif ml_home_drift < -0.02: trend = "away_strengthening"
            results.append({
                "market_slug": slug,
                "samples_used": len(recent),
                "odds_volatility": {
                    "ml_home_std": home_std,
                    "ml_away_std": away_std,
                    "max_volatility": max(home_std, away_std),
                },
                "drift_rate": {
                    "ml_home_drift_30s": ml_home_drift,
                    "trend": trend,
                },
                "market_efficiency": {
                    "vig_mean": vig_mean,
                    "vig_std": vig_std,
                    "efficiency_score": efficiency,
                },
                "last_sample": samples[-1],
            })

        # 持仓质量评分（per 持仓的综合指标）
        position_quality: list[dict] = []
        if self.runtime and self.runtime.paper_ledger:
            ledger = self.runtime.paper_ledger
            for tok, shares in ledger.positions.items():
                if shares <= Decimal("0"): continue
                cost = ledger.cost_basis_usdc.get(tok, Decimal("0"))
                entry_price = cost / shares if shares > 0 else Decimal("0")
                history = ledger.position_price_history.get(tok, [])
                first_fill = ledger.first_fill_at.get(tok)
                from datetime import datetime, timezone
                holding_seconds = (datetime.now(timezone.utc) - first_fill).total_seconds() if first_fill else 0
                max_loss = ledger.max_unrealized_loss.get(tok, Decimal("0"))
                # 价格趋势：history 头尾对比
                price_trend = "unknown"
                drift_pct = None
                if len(history) >= 2:
                    try:
                        first_bid = Decimal(str(history[0][1]))
                        last_bid = Decimal(str(history[-1][1]))
                        if first_bid > 0:
                            drift_pct = round(float((last_bid - first_bid) / first_bid * 100), 2)
                            if drift_pct > 2: price_trend = "rising"
                            elif drift_pct < -2: price_trend = "falling"
                            else: price_trend = "stable"
                    except Exception: pass
                # 持仓质量评分（0-100）：未亏损 + 价格上行 + 持有不超过 4h
                quality = 50
                if max_loss >= Decimal("0"): quality += 20
                if price_trend == "rising": quality += 20
                elif price_trend == "falling": quality -= 20
                if holding_seconds > 14400: quality -= 20  # 4h+
                quality = max(0, min(100, quality))
                position_quality.append({
                    "token_id": tok[:32],
                    "shares": str(shares),
                    "entry_price": str(entry_price.quantize(Decimal("0.0001"))),
                    "holding_seconds": round(holding_seconds, 0),
                    "holding_hours": round(holding_seconds / 3600, 2),
                    "max_drawdown_usdc": str(max_loss),
                    "price_trend": price_trend,
                    "drift_pct": drift_pct,
                    "quality_score": quality,
                })

        # 汇总
        all_vols = [r["odds_volatility"]["max_volatility"] for r in results if r["odds_volatility"]["max_volatility"]]
        all_effs = [r["market_efficiency"]["efficiency_score"] for r in results if r["market_efficiency"]["efficiency_score"]]
        all_quality = [p["quality_score"] for p in position_quality]
        return {
            "markets_analyzed": len(results),
            "positions_count": len(position_quality),
            "summary": {
                "avg_odds_volatility": round(mean(all_vols), 4) if all_vols else None,
                "max_odds_volatility": max(all_vols) if all_vols else None,
                "avg_market_efficiency": round(mean(all_effs), 3) if all_effs else None,
                "avg_position_quality": round(mean(all_quality), 1) if all_quality else None,
            },
            "markets": sorted(results, key=lambda x: -x["odds_volatility"]["max_volatility"])[:30],
            "position_quality": position_quality,
        }

    async def error_rate_timeseries(self, window_minutes: int = 15) -> dict[str, object]:
        """错误率时序 — 最近 1/5/15min HTTP errors + audit errors + per-bucket。

        分 1min 桶聚合，找出错误暴增时段。
        """
        if self.runtime is None or self.runtime.db_session_factory is None:
            return {"buckets": [], "summary": {}}
        from sqlalchemy import text as sql_text
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        try:
            async with self.runtime.db_session_factory() as session:
                rows = (await session.execute(
                    sql_text("""
                        SELECT
                          date_trunc('minute', created_at) as bucket,
                          event_title,
                          COUNT(*) FILTER (WHERE status IN ('rejected','failed','error') OR reason LIKE '%error%' OR reason LIKE '%failed%') as errors,
                          COUNT(*) as total
                        FROM audit_events
                        WHERE created_at > :cutoff
                          AND event_title IN ('order_rejected','order_state_updated','order_submitted','risk_rejection_recorded','fill_recorded')
                        GROUP BY 1, 2
                        ORDER BY 1 DESC
                    """),
                    {"cutoff": cutoff},
                )).all()
        except Exception as exc:
            return {"error": str(exc)}
        from collections import defaultdict
        by_bucket: dict[str, dict] = defaultdict(lambda: {"errors": 0, "total": 0, "by_event": {}})
        for r in rows:
            b = r[0].isoformat()
            by_bucket[b]["errors"] += int(r[2] or 0)
            by_bucket[b]["total"] += int(r[3] or 0)
            by_bucket[b]["by_event"][r[1]] = {"errors": int(r[2] or 0), "total": int(r[3] or 0)}
        buckets = []
        for b in sorted(by_bucket.keys(), reverse=True):
            bk = by_bucket[b]
            buckets.append({
                "bucket": b,
                "errors": bk["errors"],
                "total": bk["total"],
                "error_rate_pct": round(bk["errors"] / bk["total"] * 100, 2) if bk["total"] else 0,
                "by_event": bk["by_event"],
            })
        # 派生 1/5/15 min 总错误率
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)
        summary = {}
        for w_min in (1, 5, 15):
            w_cutoff = now - timedelta(minutes=w_min)
            w_errors = sum(b["errors"] for b in buckets if datetime.fromisoformat(b["bucket"].replace("+00:00", "+00:00")) > w_cutoff)
            w_total = sum(b["total"] for b in buckets if datetime.fromisoformat(b["bucket"].replace("+00:00", "+00:00")) > w_cutoff)
            summary[f"last_{w_min}min"] = {
                "errors": w_errors,
                "total": w_total,
                "error_rate_pct": round(w_errors / w_total * 100, 2) if w_total else 0,
            }
        return {
            "window_minutes": window_minutes,
            "buckets_count": len(buckets),
            "summary": summary,
            "buckets": buckets[:30],
        }

    def data_staleness_snapshot(self) -> dict[str, object]:
        """每个数据源的 lag/staleness 分位（用 inplay client 已有 per-sport status）。"""
        if self.runtime is None:
            return {"sources": []}
        result: list[dict[str, object]] = []
        # goalserve inplay client 提供 per sport status
        # 用 supervisor.workers_snapshot 拿各 client status（含 server_clock_lag）
        try:
            sup = self.runtime.supervisor
            if sup and hasattr(sup, "workers_snapshot"):
                wsn = sup.workers_snapshot()
                for w in wsn.get("workers", []) if isinstance(wsn, dict) else wsn:
                    name = w.get("name") if isinstance(w, dict) else None
                    if not name: continue
                    result.append({
                        "source": name,
                        "status": w.get("status") if isinstance(w, dict) else None,
                        "last_heartbeat_at": w.get("last_heartbeat_at") if isinstance(w, dict) else None,
                    })
        except Exception:
            pass
        # market_ws_worker.last_message_at
        if self.runtime.market_ws_worker:
            try:
                ws_st = self.runtime.market_ws_worker.status_snapshot(include_subscriptions=False)
                from datetime import datetime as _dt, timezone as _tz
                now = _dt.now(_tz.utc)
                lag_s = None
                if ws_st.last_message_at:
                    lag_s = round((now - ws_st.last_message_at).total_seconds(), 2)
                result.append({
                    "source": "polymarket_market_ws",
                    "connected": ws_st.connected,
                    "last_message_at": ws_st.last_message_at.isoformat() if ws_st.last_message_at else None,
                    "lag_seconds": lag_s,
                    "stale": (lag_s is not None and lag_s > 30),
                })
            except Exception as exc:
                result.append({"source": "polymarket_market_ws", "error": str(exc)})
        return {
            "sources_count": len(result),
            "healthy_count": sum(1 for r in result if not r.get("stale")),
            "stale_count": sum(1 for r in result if r.get("stale")),
            "sources": result,
        }

    async def system_perf_snapshot(self) -> dict[str, object]:
        """全系统性能 + DB 健康 + 启动耗时统一可观测视图。

        含:
        - boot_phase_timings: 各 bootstrap phase 耗时
        - process_metrics: CPU/RSS/VMS/threads/connections/ctx_switches/io
        - system_metrics: CPU per core / load avg / memory / disk / net io
        - asyncio_metrics: pending tasks
        - http_endpoints: top endpoints by call_count + latency p50/p90/p99
        - ws_traffic: per channel msg rate + bandwidth
        - db_queries: count + latency 分位
        - db_pool: asyncpg pool size/free/used connections
        """
        from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
        perf = SystemPerfMonitor.get()
        snapshot = perf.snapshot()
        # DB pool 健康
        db_pool = {}
        if self.runtime and self.runtime.db_engine:
            try:
                pool = self.runtime.db_engine.pool
                db_pool = {
                    "pool_class": type(pool).__name__,
                    "size": getattr(pool, "size", lambda: None)(),
                    "checked_in": getattr(pool, "checkedin", lambda: None)(),
                    "checked_out": getattr(pool, "checkedout", lambda: None)(),
                    "overflow": getattr(pool, "overflow", lambda: None)(),
                }
            except Exception as exc:
                db_pool = {"error": str(exc)}
        snapshot["db_pool"] = db_pool
        # Thread / Process pool 占用（concurrent.futures.Executor 的内部 _work_queue）
        executor_status = {}
        try:
            tp = self.runtime.trading_thread_pool
            if tp:
                executor_status["trading_thread_pool"] = {
                    "max_workers": tp._max_workers,
                    "active_threads": len(tp._threads) if hasattr(tp, '_threads') else None,
                    "queued_tasks": tp._work_queue.qsize() if hasattr(tp, '_work_queue') else None,
                    "shutdown": tp._shutdown,
                }
        except Exception as exc:
            executor_status["trading_thread_pool"] = {"error": str(exc)}
        try:
            mp = self.runtime.maintenance_thread_pool
            if mp:
                executor_status["maintenance_thread_pool"] = {
                    "max_workers": mp._max_workers,
                    "active_threads": len(mp._threads) if hasattr(mp, '_threads') else None,
                    "queued_tasks": mp._work_queue.qsize() if hasattr(mp, '_work_queue') else None,
                }
        except Exception as exc:
            executor_status["maintenance_thread_pool"] = {"error": str(exc)}
        try:
            pp = self.runtime.maintenance_process_pool
            if pp:
                executor_status["maintenance_process_pool"] = {
                    "max_workers": pp._max_workers,
                    "active_processes": len(pp._processes) if hasattr(pp, '_processes') else None,
                    "pending_work_items": len(pp._pending_work_items) if hasattr(pp, '_pending_work_items') else None,
                }
        except Exception as exc:
            executor_status["maintenance_process_pool"] = {"error": str(exc)}
        snapshot["executor_pools"] = executor_status
        # Event bus 队列水位
        if self.runtime.event_bus and hasattr(self.runtime.event_bus, "snapshot"):
            try:
                qs = self.runtime.event_bus.snapshot()
                snapshot["event_bus_queues"] = {
                    "trading_depth": qs.trading_queue_depth,
                    "trading_capacity": qs.trading_queue_capacity,
                    "trading_util_pct": round(qs.trading_queue_depth / qs.trading_queue_capacity * 100, 1) if qs.trading_queue_capacity else 0,
                    "maintenance_depth": qs.maintenance_queue_depth,
                    "maintenance_capacity": qs.maintenance_queue_capacity,
                    "persistence_depth": qs.persistence_queue_depth,
                    "persistence_capacity": qs.persistence_queue_capacity,
                    "low_priority_paused": qs.low_priority_paused,
                }
                if hasattr(self.runtime.event_bus, "mirror_failure_count"):
                    snapshot["event_bus_queues"]["mirror_failure_count"] = self.runtime.event_bus.mirror_failure_count()
            except Exception as exc:
                snapshot["event_bus_queues"] = {"error": str(exc)}
        # Outbox 真实 pending
        if self.runtime.outbox and hasattr(self.runtime.outbox, "snapshot"):
            try:
                ready, retained, dead = self.runtime.outbox.snapshot()
                snapshot["outbox"] = {
                    "ready_count": ready,
                    "retained_count": retained,
                    "dead_letter_count": dead,
                }
            except Exception as exc:
                snapshot["outbox"] = {"error": str(exc)}
        # DB ping latency — 拆 pool checkout / execute / commit 三段定位排队 vs query
        if self.runtime and self.runtime.db_session_factory:
            import time as _t
            from sqlalchemy import text as _text
            try:
                t0 = _t.perf_counter()
                session_ctx = self.runtime.db_session_factory()
                session = await session_ctx.__aenter__()
                t_checkout = (_t.perf_counter() - t0) * 1000
                t1 = _t.perf_counter()
                await session.execute(_text("SELECT 1"))
                t_execute = (_t.perf_counter() - t1) * 1000
                t2 = _t.perf_counter()
                await session_ctx.__aexit__(None, None, None)
                t_close = (_t.perf_counter() - t2) * 1000
                total = (_t.perf_counter() - t0) * 1000
                snapshot["db_ping_ms"] = round(total, 2)
                snapshot["db_ping_breakdown"] = {
                    "pool_checkout_ms": round(t_checkout, 2),
                    "execute_ms": round(t_execute, 2),
                    "session_close_ms": round(t_close, 2),
                }
            except Exception as exc:
                snapshot["db_ping_ms"] = None
                snapshot["db_ping_error"] = str(exc)
        return snapshot

    def memory_timeseries_snapshot(self) -> dict[str, object]:
        """进程 RSS 时间序列(每 30s 一点,保留 1h = 120 点)+ 派生指标."""
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

    def memory_components_snapshot(self) -> dict[str, object]:
        """各 runtime store/buffer 内存估算(condition_count + 估算 KB)."""
        if self.runtime is None:
            return {"available": False}
        components: dict[str, dict[str, object]] = {}
        # orderbook_history_buffer (自带 memory_footprint_estimate)
        if self.runtime.orderbook_history_buffer is not None:
            try:
                components["orderbook_history_buffer"] = self.runtime.orderbook_history_buffer.memory_footprint_estimate()
            except Exception as exc:
                components["orderbook_history_buffer"] = {"error": str(exc)[:120]}
        # orderbook_delta_store
        if self.runtime.orderbook_delta_store is not None:
            try:
                ds = self.runtime.orderbook_delta_store
                tokens = ds.tracked_tokens()
                total_samples = sum(len(ds.samples(t)) for t in tokens)
                components["orderbook_delta_store"] = {
                    "tracked_tokens": len(tokens),
                    "total_samples": total_samples,
                    "estimated_kb": round(total_samples * 0.4, 1),  # 每 sample ~400B
                }
            except Exception as exc:
                components["orderbook_delta_store"] = {"error": str(exc)[:120]}
        # market_ws_worker._states
        if self.runtime.market_ws_worker is not None:
            try:
                ws = self.runtime.market_ws_worker
                states_count = len(ws._states) if hasattr(ws, "_states") else 0
                components["market_ws_worker_states"] = {
                    "tracked_count": states_count,
                    "estimated_kb": round(states_count * 1.0, 1),  # 每 state ~1KB
                }
            except Exception as exc:
                components["market_ws_worker_states"] = {"error": str(exc)[:120]}
        # entry_metadata_store
        if self.runtime.entry_metadata_store is not None:
            try:
                records = list(self.runtime.entry_metadata_store.records())
                components["entry_metadata_store"] = {
                    "records": len(records),
                    "estimated_kb": round(len(records) * 2.0, 1),
                }
            except Exception as exc:
                components["entry_metadata_store"] = {"error": str(exc)[:120]}
        # account_state_store
        if self.runtime.account_state_store is not None:
            try:
                acc = self.runtime.account_state_store.snapshot()
                components["account_state_store"] = {
                    "open_orders": len(acc.open_orders),
                    "fills": len(acc.fills),
                    "positions": len(acc.positions),
                    "market_pauses": len(acc.market_pauses),
                }
            except Exception as exc:
                components["account_state_store"] = {"error": str(exc)[:120]}
        # paper_ledger
        if self.runtime.paper_ledger is not None:
            try:
                pl = self.runtime.paper_ledger
                positions_count = len(pl.positions)
                price_history_samples = sum(len(h) for h in getattr(pl, "position_price_history", {}).values())
                equity_curve_len = len(getattr(pl, "equity_curve", []))
                realized_trades_len = len(getattr(pl, "realized_trades", []))
                components["paper_ledger"] = {
                    "positions": positions_count,
                    "position_price_samples": price_history_samples,
                    "equity_curve_points": equity_curve_len,
                    "realized_trades": realized_trades_len,
                }
            except Exception as exc:
                components["paper_ledger"] = {"error": str(exc)[:120]}
        # registry
        if self.runtime.registry is not None:
            try:
                snap = self.runtime.registry.snapshot()
                components["market_registry"] = {
                    "markets": len(snap.markets),
                    "estimated_kb": round(len(snap.markets) * 4.0, 1),
                }
            except Exception as exc:
                components["market_registry"] = {"error": str(exc)[:120]}
        # 当前进程 RSS 作总对照
        try:
            import resource
            import platform
            ru = resource.getrusage(resource.RUSAGE_SELF)
            rss_bytes = ru.ru_maxrss if platform.system() == "Darwin" else ru.ru_maxrss * 1024
            total_rss_mb = round(rss_bytes / 1024 / 1024, 1)
        except Exception:
            total_rss_mb = None
        return {
            "available": True,
            "total_process_rss_mb": total_rss_mb,
            "components": components,
        }

    def memory_objects_snapshot(self, top_n: int = 30) -> dict[str, object]:
        """gc.get_objects() 按 type 分组 top N,识别哪类对象占用最多."""
        import gc
        from collections import Counter
        counts: Counter[str] = Counter()
        try:
            for obj in gc.get_objects():
                counts[type(obj).__name__] += 1
        except Exception as exc:
            return {"available": False, "error": str(exc)[:200]}
        top = counts.most_common(top_n)
        return {
            "available": True,
            "total_objects": sum(counts.values()),
            "unique_types": len(counts),
            "top_types": [{"type": name, "count": cnt} for name, cnt in top],
        }

    def data_sources_health(self) -> dict[str, object]:
        """所有数据源的延迟/健康可观测性聚合。

        汇总所有 client/worker 的 latency + success rate + last_error：
        - goalserve_inplay: per sport poll_age / server_clock_lag / transport_lag / 429 退避
        - goalserve_livescore: per sport poll_age / errors
        - goalserve_pregame: poll_age
        - mlb/nba play-by-play: fetch_count / error_count / last_fetched_at / last_error
        - goalserve_lazy: cache entries + age
        - market_ws (polymarket): subscription_count / connected / last_message_at
        - user_ws: connected / last_message_at
        - persistence outbox: 积压
        """
        if self.runtime is None:
            return {"error": "runtime not bound"}
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        health: dict[str, object] = {"checked_at": now.isoformat()}

        # Goalserve lazy（schedule/standings/h2h cache）
        if self.runtime.goalserve_lazy_client:
            health["goalserve_lazy"] = self.runtime.goalserve_lazy_client.cache_status()

        # Market WS
        if self.runtime.market_ws_worker:
            try:
                st = self.runtime.market_ws_worker.status_snapshot(include_subscriptions=False)
                health["polymarket_market_ws"] = {
                    "tracked_markets": st.tracked_market_count,
                    "subscription_count": st.subscription_count,
                    "connected": st.connected,
                    "last_message_at": st.last_message_at.isoformat() if st.last_message_at else None,
                    "last_rest_snapshot_at": st.last_rest_snapshot_at.isoformat() if st.last_rest_snapshot_at else None,
                    "last_error": st.last_error,
                    "lag_since_last_message_s": (
                        round((now - st.last_message_at).total_seconds(), 2) if st.last_message_at else None
                    ),
                }
            except Exception as exc:
                health["polymarket_market_ws"] = {"error": str(exc)}

        # User WS
        if self.runtime.user_ws_worker:
            try:
                user_ws = self.runtime.user_ws_worker
                health["polymarket_user_ws"] = {
                    "connected": getattr(user_ws, "_is_connected", None),
                    "last_message_at": (
                        last.isoformat() if (last := getattr(user_ws, "_last_message_at", None)) else None
                    ),
                }
            except Exception as exc:
                health["polymarket_user_ws"] = {"error": str(exc)}

        # Outbox 积压
        if self.runtime.outbox:
            try:
                health["outbox"] = {
                    "pending_count": getattr(self.runtime.outbox, "pending_count", lambda: None)(),
                }
            except Exception:
                pass

        # Sports inplay / livescore（通过 supervisor 拿状态）
        try:
            sup = self.runtime.supervisor
            if sup and hasattr(sup, "workers_snapshot"):
                workers = sup.workers_snapshot()
                health["workers_snapshot"] = workers
        except Exception:
            pass

        # === per-sport 数据源详情(每个 sport 的 inplay/livescore poll lag + events) ===
        try:
            if self.runtime.sports_live_state_client is not None:
                per_sport = self.runtime.sports_live_state_client.source_detail_status()
                # 按 sport 聚合(每 sport 可能有 inplay + livescore 2 个源)
                sport_summary: dict[str, list[dict]] = {}
                for s in per_sport:
                    sp = s.get("sport") or "unknown"
                    sport_summary.setdefault(sp, []).append(s)
                health["per_sport_sources"] = sport_summary
        except Exception as exc:
            health["per_sport_sources_error"] = str(exc)[:120]

        # === 直播对局/比分/赔率 整体新鲜度桶分布 ===
        # 从 entry_metadata_store 拉所有 record 的 updated_at,分桶统计
        try:
            store = self._entry_metadata_store() if hasattr(self, "_entry_metadata_store") else None
            if store is None:
                store = self.runtime.entry_metadata_store
            if store is not None:
                buckets = {"<5s": 0, "5-30s": 0, "30-60s": 0, "60-300s": 0, ">300s": 0, "no_state": 0}
                total = 0; signal_allowed_count = 0
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
                    if age_s < 5: buckets["<5s"] += 1
                    elif age_s < 30: buckets["5-30s"] += 1
                    elif age_s < 60: buckets["30-60s"] += 1
                    elif age_s < 300: buckets["60-300s"] += 1
                    else: buckets[">300s"] += 1
                live_state_summary: dict[str, object] = {
                    "total_markets_with_metadata": total,
                    "signal_allowed_count": signal_allowed_count,
                    "freshness_buckets": buckets,
                }
                if ages_ms:
                    sorted_ages = sorted(ages_ms)
                    n = len(sorted_ages)
                    live_state_summary["age_ms_p50"] = round(sorted_ages[n // 2], 0)
                    live_state_summary["age_ms_p90"] = round(sorted_ages[min(int(n * 0.9), n - 1)], 0)
                    live_state_summary["age_ms_p99"] = round(sorted_ages[min(int(n * 0.99), n - 1)], 0)
                    live_state_summary["age_ms_max"] = round(sorted_ages[-1], 0)
                    live_state_summary["age_ms_avg"] = round(sum(sorted_ages) / n, 0)
                health["live_state_freshness"] = live_state_summary
        except Exception as exc:
            health["live_state_freshness_error"] = str(exc)[:120]

        # === Orderbook snapshot 新鲜度桶(per token 最后 snapshot age) ===
        try:
            if self.runtime.market_ws_worker is not None:
                ws = self.runtime.market_ws_worker
                ob_buckets = {"<1s": 0, "1-5s": 0, "5-30s": 0, "30-60s": 0, ">60s": 0}
                total_tokens = 0
                ob_ages_ms: list[float] = []
                for token_id, state in (getattr(ws, "_states", {}) or {}).items():
                    snap = getattr(state, "snapshot", None)
                    if snap is None: continue
                    total_tokens += 1
                    age_s = (now - snap.received_at).total_seconds()
                    ob_ages_ms.append(age_s * 1000)
                    if age_s < 1: ob_buckets["<1s"] += 1
                    elif age_s < 5: ob_buckets["1-5s"] += 1
                    elif age_s < 30: ob_buckets["5-30s"] += 1
                    elif age_s < 60: ob_buckets["30-60s"] += 1
                    else: ob_buckets[">60s"] += 1
                ob_summary: dict[str, object] = {
                    "total_tokens": total_tokens,
                    "freshness_buckets": ob_buckets,
                }
                if ob_ages_ms:
                    sorted_ages = sorted(ob_ages_ms)
                    n = len(sorted_ages)
                    ob_summary["age_ms_p50"] = round(sorted_ages[n // 2], 0)
                    ob_summary["age_ms_p90"] = round(sorted_ages[min(int(n * 0.9), n - 1)], 0)
                    ob_summary["age_ms_max"] = round(sorted_ages[-1], 0)
                health["orderbook_freshness"] = ob_summary
        except Exception as exc:
            health["orderbook_freshness_error"] = str(exc)[:120]

        # Reconcile 状态（最近 refresh）
        if self.runtime.account_state_store:
            try:
                acc = self.runtime.account_state_store.snapshot()
                health["account_state"] = {
                    "last_reconcile_at": acc.last_reconcile_at.isoformat() if acc.last_reconcile_at else None,
                    "reconcile_age_seconds": (
                        round((now - acc.last_reconcile_at).total_seconds(), 1)
                        if acc.last_reconcile_at else None
                    ),
                }
            except Exception as exc:
                health["account_state"] = {"error": str(exc)}

        # Trade tape cache 状态
        from polymarket_trader.app.admin_service import _TRADE_TAPE_CACHE
        import time
        if _TRADE_TAPE_CACHE:
            ages = [round(time.time() - ts, 1) for ts, _ in _TRADE_TAPE_CACHE.values()]
            health["polymarket_trade_tape_cache"] = {
                "entries": len(_TRADE_TAPE_CACHE),
                "min_age_seconds": min(ages) if ages else None,
                "max_age_seconds": max(ages) if ages else None,
            }

        return health

    def odds_drift_snapshot(self, market_slug: str | None = None, limit: int = 100) -> dict[str, object]:
        """Goalserve 赔率漂移时序（每 market 5s 采样 1 次，最多保留 200 点）。

        - market_slug 为空：返回所有 tracked market 的简要统计
        - 含 market_slug：返回该 market 完整时序 + 派生漂移率
        派生：
        - ml_home_p_change_5min: 最近 5min 内 home 隐含概率变化
        - vig_pct_current: 当前 ML overround
        - vig_change_5min: vig 变化（庄家收紧 / 放松信号）
        """
        from polymarket_trader.workers.trading_decision.worker import get_odds_drift_store
        store = get_odds_drift_store()
        if market_slug:
            samples = list(store.get(market_slug, []))[-limit:]
            if not samples:
                return {"market_slug": market_slug, "samples": [], "samples_count": 0}
            # 派生漂移指标
            first, last = samples[0], samples[-1]
            from datetime import datetime
            try:
                first_at = datetime.fromisoformat(first["at"])
                last_at = datetime.fromisoformat(last["at"])
                duration_s = (last_at - first_at).total_seconds()
            except Exception:
                duration_s = 0
            def _diff(key: str) -> float | None:
                a, b = first.get(key), last.get(key)
                if a is None or b is None: return None
                return round(float(b) - float(a), 4)
            return {
                "market_slug": market_slug,
                "samples_count": len(samples),
                "duration_seconds": round(duration_s, 1),
                "first_sample": first,
                "last_sample": last,
                "ml_home_drift": _diff("ml_home_p"),
                "ml_away_drift": _diff("ml_away_p"),
                "tt_over_drift": _diff("tt_over_p"),
                "sp_home_drift": _diff("sp_home_p"),
                "samples": samples,
            }
        # all markets 摘要
        summary = []
        for slug, dq in store.items():
            samples = list(dq)
            if not samples: continue
            summary.append({
                "market_slug": slug,
                "samples_count": len(samples),
                "first_at": samples[0]["at"],
                "last_at": samples[-1]["at"],
            })
        summary.sort(key=lambda x: x["last_at"], reverse=True)
        return {
            "tracked_markets": len(summary),
            "summary": summary[:50],
        }

    async def soccer_injuries_snapshot(self) -> dict[str, object] | None:
        if self.runtime is None or self.runtime.goalserve_lazy_client is None:
            return None
        return await self.runtime.goalserve_lazy_client.soccer_injuries()

    def arbitrage_snapshot(self) -> dict[str, object]:
        """同 event 跨盘口套利检测 — 隐含概率和应该 ≤ 1 + vig。

        逻辑：
        - 同 event_slug 内所有 condition_id 的 outcome 必互斥（一边赢另一边输）
        - 真实概率 sum = 1.0；market 价格 sum = 1.0 + vig（健康 5-10%）
        - **sum < 1.0 = 套利机会**（买所有 outcome 必赚 vig）
        - **sum > 1.20 = 异常分歧**（市场极度混乱）

        遍历 registry 同 event_slug 的所有 market，按 condition_id 取每个
        outcome 当前 best_ask，求和。
        """
        if self.runtime is None or self.runtime.registry is None or self.runtime.market_ws_worker is None:
            return {"events": [], "checked_count": 0}
        from decimal import Decimal
        from collections import defaultdict
        registry = self.runtime.registry
        ws = self.runtime.market_ws_worker
        markets = registry.snapshot().markets
        # 按 event_slug 分组
        by_event: dict[str, list] = defaultdict(list)
        for m in markets:
            if not m.event_slug or m.trading_status != "eligible":
                continue
            by_event[m.event_slug].append(m)
        results: list[dict] = []
        for event_slug, ms in by_event.items():
            if len(ms) < 2:
                continue
            # 只看 ML 类（单一 condition_id 的 outcomes 都是独立 token，互斥）
            # 检查每个 market 的所有 outcomes
            for market in ms:
                if len(market.token_ids) < 2:
                    continue
                ask_sum = Decimal("0")
                bid_sum = Decimal("0")
                outcomes_data = []
                valid = True
                for token_id, outcome_label in zip(market.token_ids, [o.outcome for o in market.outcomes]):
                    ob = ws.snapshot(token_id)
                    if ob is None or ob.best_ask is None:
                        valid = False
                        break
                    ask_sum += ob.best_ask
                    if ob.best_bid is not None:
                        bid_sum += ob.best_bid
                    outcomes_data.append({
                        "outcome": outcome_label,
                        "best_ask": str(ob.best_ask),
                        "best_bid": str(ob.best_bid) if ob.best_bid else None,
                    })
                if not valid:
                    continue
                # 套利信号
                arb_signal = None
                if ask_sum < Decimal("1.0"):
                    arb_signal = "buy_all_arbitrage"  # 买所有 outcome 必赚
                elif ask_sum > Decimal("1.20"):
                    arb_signal = "high_vig_anomaly"
                if arb_signal or (Decimal("0.95") <= ask_sum <= Decimal("1.10")):
                    results.append({
                        "event_slug": event_slug,
                        "market_slug": market.market_slug,
                        "condition_id": market.condition_id[:10],
                        "outcomes_count": len(outcomes_data),
                        "ask_sum": str(ask_sum.quantize(Decimal("0.0001"))),
                        "bid_sum": str(bid_sum.quantize(Decimal("0.0001"))) if bid_sum > 0 else None,
                        "vig_pct": str(((ask_sum - Decimal("1")) * 100).quantize(Decimal("0.01"))),
                        "arb_signal": arb_signal,
                        "outcomes": outcomes_data,
                    })
        # 按 vig 升序排，套利机会（vig <0）在前
        results.sort(key=lambda x: float(x["vig_pct"]))
        return {
            "checked_events": len(by_event),
            "results_count": len(results),
            "arbitrage_opportunities": [r for r in results if r["arb_signal"] == "buy_all_arbitrage"],
            "anomalies": [r for r in results if r["arb_signal"] == "high_vig_anomaly"],
            "all_results": results[:30],
        }

    def live_attention_snapshot(self) -> dict[str, object]:
        """同时 LIVE 比赛数 + per sport 分布（注意力分散度量化指标）。

        基于 sports_live_state_store / market_ws_worker 综合得出当前 LIVE
        市场的 sport 分布 + 总数。同时 LIVE 越多，市场注意力越分散 → 单 market
        信号噪音越大。
        """
        if self.runtime is None or self.runtime.market_ws_worker is None:
            return {"tracked_markets": 0, "by_sport": {}}
        ws = self.runtime.market_ws_worker
        tracked = ws._tracked_markets if hasattr(ws, "_tracked_markets") else {}
        from collections import Counter
        sport_count: Counter = Counter()
        for token_id, market in (tracked.items() if hasattr(tracked, "items") else []):
            slug = (market.market_slug or "").lower() if hasattr(market, "market_slug") else ""
            sport = "other"
            for s in ("kbo", "mlb", "nba", "wnba", "nhl", "nfl", "ncaaf", "ncaab",
                      "atp", "wta", "itf", "mls", "epl", "j2100", "j1100"):
                if s in slug:
                    sport = s; break
            sport_count[sport] += 1
        return {
            "tracked_markets": len(tracked) if hasattr(tracked, "__len__") else 0,
            "by_sport": dict(sport_count),
            "unique_sports": len(sport_count),
            "max_sport": sport_count.most_common(1)[0] if sport_count else None,
            "attention_dispersion_score": round(len(sport_count) / max(1, len(tracked)) * 100, 2),
        }

    async def win_rate_breakdown(self, window_hours: int = 168) -> dict[str, object]:
        """历史胜率分组（per sport / market_type / 价位区间）。

        基于已平仓 BUY+SELL 配对计算 realized PnL，多维度分组聚合。窗口默认 7 天。

        分组维度：
        - buy_price_bucket（0.0-0.2/.../0.8-1.0）
        - sport（kbo/mlb/nba/atp/wta/itf/mls/j2100/...）—— 从 market_slug 推断
        - market_type（ML/totals/spreads/分段 prop）—— 从 market_slug 推断
        - sport × price_bucket 交叉（找 sport 内最赚 / 最亏价位）

        指标：trade_count / win_count / winrate / avg_pnl / total_pnl /
              avg_win / avg_loss / profit_factor (Σwin / |Σloss|)
        """
        if self.runtime is None or self.runtime.db_session_factory is None:
            return {"groups": [], "window_hours": window_hours}
        from sqlalchemy import text as sql_text
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        try:
            async with self.runtime.db_session_factory() as session:
                # 拉所有 BUY+SELL fill + market_slug，按 token 配对算 realized
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
                        LEFT JOIN markets m ON m.condition_id = a.payload->'fill'->>'condition_id'
                        WHERE a.event_title='fill_recorded'
                          AND a.created_at > :cutoff
                          AND a.payload->'fill' IS NOT NULL
                        ORDER BY a.created_at
                    """),
                    {"cutoff": cutoff},
                )
                fills = list(result)
        except Exception as exc:
            return {"error": str(exc), "groups": [], "window_hours": window_hours}

        # 配对 BUY+SELL：FIFO，按 token 维护持仓 + cost_basis（含 market_slug）
        from collections import defaultdict
        from decimal import Decimal
        positions: dict[str, list] = defaultdict(list)
        closed_trades = []
        token_to_slug: dict[str, str] = {}
        for row in fills:
            side = (row[0] or "").lower()
            token = row[1] or ""
            price = Decimal(str(row[2])) if row[2] is not None else Decimal("0")
            size = Decimal(str(row[3])) if row[3] is not None else Decimal("0")
            notional = Decimal(str(row[4])) if row[4] is not None else Decimal("0")
            slug = row[5] or ""
            if slug: token_to_slug[token] = slug
            if size <= 0: continue
            if side == "buy":
                positions[token].append({"cost": notional, "shares": size, "price": price})
            elif side == "sell":
                remaining_to_sell = size
                while remaining_to_sell > 0 and positions[token]:
                    buy = positions[token][0]
                    use_shares = min(buy["shares"], remaining_to_sell)
                    cost_ratio = use_shares / buy["shares"] if buy["shares"] > 0 else Decimal("0")
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
                    if buy["shares"] <= Decimal("0.0000001"):
                        positions[token].pop(0)
                    remaining_to_sell -= use_shares

        # 三个维度的分组
        def _price_bucket(p: float) -> str:
            if p < 0.2: return "0.0-0.2"
            if p < 0.4: return "0.2-0.4"
            if p < 0.6: return "0.4-0.6"
            if p < 0.8: return "0.6-0.8"
            return "0.8-1.0"

        def _sport_from_slug(slug: str) -> str:
            sl = slug.lower()
            for s in ("kbo", "mlb", "nba", "wnba", "nhl", "nfl", "ncaaf", "ncaab",
                      "atp", "wta", "itf", "mls", "epl", "laliga", "j2100", "j1100",
                      "j3100", "chi", "bra", "arg", "mex"):
                if s in sl: return s
            return "other"

        def _market_type_from_slug(slug: str) -> str:
            sl = slug.lower()
            if "spread" in sl: return "spread"
            if "total" in sl: return "total"
            if "halftime" in sl: return "halftime"
            if "first-set" in sl or "set-winner" in sl: return "set_prop"
            if "exact-score" in sl or "correct-score" in sl: return "exact_score"
            if "nrfi" in sl: return "nrfi"
            if "winner" in sl or sl.count("-") <= 3: return "moneyline"
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
                "profit_factor": round(sum(wins) / abs(total_loss), 3) if total_loss < 0 else None,
            }

        by_bucket: dict[str, list[float]] = defaultdict(list)
        by_sport: dict[str, list[float]] = defaultdict(list)
        by_type: dict[str, list[float]] = defaultdict(list)
        by_sport_bucket: dict[tuple[str, str], list[float]] = defaultdict(list)
        for trade in closed_trades:
            bucket = _price_bucket(trade["buy_price"])
            sport = _sport_from_slug(trade["market_slug"])
            mtype = _market_type_from_slug(trade["market_slug"])
            by_bucket[bucket].append(trade["pnl"])
            by_sport[sport].append(trade["pnl"])
            by_type[mtype].append(trade["pnl"])
            by_sport_bucket[(sport, bucket)].append(trade["pnl"])

        groups_bucket = [_aggregate(p, "buy_price_bucket", b) for b, p in sorted(by_bucket.items())]
        groups_sport = sorted(
            (_aggregate(p, "sport", s) for s, p in by_sport.items()),
            key=lambda g: g["total_pnl"], reverse=True,
        )
        groups_type = sorted(
            (_aggregate(p, "market_type", t) for t, p in by_type.items()),
            key=lambda g: g["total_pnl"], reverse=True,
        )
        groups_sport_bucket = sorted(
            (_aggregate(p, "sport_x_price", f"{s}/{b}") for (s, b), p in by_sport_bucket.items()),
            key=lambda g: g["total_pnl"], reverse=True,
        )

        return {
            "window_hours": window_hours,
            "total_fills": len(fills),
            "closed_trades": len(closed_trades),
            "open_positions_count": sum(1 for tok, q in positions.items() if q),
            "total_realized_pnl": round(sum(t["pnl"] for t in closed_trades), 4),
            "by_price_bucket": groups_bucket,
            "by_sport": groups_sport,
            "by_market_type": groups_type,
            "by_sport_x_price": groups_sport_bucket[:20],
            "recent_trades_sample": closed_trades[-10:],
        }

    async def guard_stats_snapshot(self, window_minutes: int = 60) -> dict[str, object]:
        """守卫触发统计：每个 reject reason 在过去 N 分钟触发的次数。

        从 audit_events 聚合 reject reason 分布，操盘能立刻看到：
        - 哪些守卫频繁触发（说明在防错）
        - 哪些守卫从未触发（可能是规则太松或场景不到）
        - 异常 reason（说明有新 bug）
        """
        if self.runtime is None or self.runtime.db_session_factory is None:
            return {"items": [], "window_minutes": window_minutes}
        from sqlalchemy import text as sql_text
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        try:
            async with self.runtime.db_session_factory() as session:
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
        except Exception as exc:
            return {"error": str(exc), "items": [], "window_minutes": window_minutes}
        items = [{"reason": r[0], "event": r[1], "count": r[2]} for r in rows]
        return {"window_minutes": window_minutes, "total": sum(i["count"] for i in items), "items": items}

    def paper_metrics_snapshot(self) -> dict[str, object] | None:
        """Paper trading 综合量化指标——一次拿全 PnL/胜率/守卫/撮合/盘口效率。

        覆盖 8 个维度,操盘/复盘无需逐 endpoint 调:
        1. 资金 - available/equity/realized_pnl/unrealized_pnl/fees
        2. 持仓 - count/avg_cost/avg_holding_seconds/max_single_loss
        3. 撮合 - BUY/SELL 成功率, full_fill/partial_fill/no_fill 分布
        4. 守卫 - 每个 reject reason 触发次数 (LATE_GAME, ORDERBOOK_BEARISH, NO_EXIT 等)
        5. 入场 - 5min 内 candidate 触发 / accept / reject 数
        6. 盘口 - tracked tokens / WS subscribed / 有 orderbook_direction 数据的 token 数
        7. 市场 - 各 sport 候选数, 各 market_type 候选数
        8. PnL 时序 - 最近 N 个 ledger 快照（需后续加 history 累积）
        """
        if self.runtime is None or self.runtime.paper_ledger is None:
            return None
        from decimal import Decimal
        ledger = self.runtime.paper_ledger
        ws = self.runtime.market_ws_worker
        # 1. 资金
        total_cost = Decimal("0")
        unrealized_value = Decimal("0")
        per_pos = []
        for tok, shares in ledger.positions.items():
            cost = ledger.cost_basis_usdc.get(tok, Decimal("0"))
            total_cost += cost
            best_bid_now = None
            if ws is not None:
                ob = ws.snapshot(tok)
                if ob is not None and ob.best_bid is not None and ob.sell_actionable:
                    best_bid_now = ob.best_bid
                    unrealized_value += shares * ob.best_bid
            per_pos.append({
                "token_id": tok[:32],
                "shares": str(shares),
                "cost_usdc": str(cost),
                "avg_price": str((cost / shares).quantize(Decimal("0.0001"))) if shares > 0 else None,
                "best_bid_now": str(best_bid_now) if best_bid_now is not None else None,
                "current_value": str(shares * best_bid_now) if best_bid_now is not None else "0",
                "unrealized_pnl": str(shares * best_bid_now - cost) if best_bid_now is not None else str(-cost),
            })
        equity = ledger.available_usdc + unrealized_value
        realized_pnl = ledger.available_usdc - self.runtime.settings.portfolio_budget_usdc + total_cost
        # 2. 撮合：从 paper client 取 simulations 统计
        execution_client = getattr(self.runtime.order_executor, "_client", None)
        sim_stats = {"total": 0, "full_fill": 0, "partial_fill": 0, "no_fill": 0, "live": 0, "rejected": 0, "other": 0}
        if execution_client is not None and hasattr(execution_client, "simulations"):
            for _, outcome in execution_client.simulations:
                sim_stats["total"] += 1
                status = str(outcome.response.status.value if hasattr(outcome.response.status, "value") else outcome.response.status).lower()
                if "full" in status: sim_stats["full_fill"] += 1
                elif "partial" in status: sim_stats["partial_fill"] += 1
                elif "no_fill" in status: sim_stats["no_fill"] += 1
                elif "live" in status: sim_stats["live"] += 1
                elif "reject" in status: sim_stats["rejected"] += 1
                else: sim_stats["other"] += 1
        # 6. 盘口
        ws_status = ws.status_snapshot(include_subscriptions=False) if ws else None
        ws_metrics = {}
        if ws_status is not None:
            ws_metrics = {
                "tracked_markets": ws_status.tracked_market_count,
                "subscription_count": ws_status.subscription_count,
                "connected": ws_status.connected,
            }
        # sport 集中度（统计指标，不参与决策）
        from collections import Counter
        sport_concentration: Counter[str] = Counter()
        for tok in ledger.positions.keys():
            market = self.runtime.registry.get_by_token_id(tok) if self.runtime.registry else None
            if market is None: continue
            # 从 market_slug 推测 sport（粗略）
            slug = (market.market_slug or "").lower()
            sport = "unknown"
            for s in ("mlb", "nba", "wnba", "nhl", "nfl", "ncaaf", "ncaab", "kbo",
                      "atp", "wta", "itf", "mls", "epl", "laliga", "j2100", "j1100"):
                if s in slug:
                    sport = s; break
            sport_concentration[sport] += 1
        max_sport = sport_concentration.most_common(1)
        max_sport_count = max_sport[0][1] if max_sport else 0
        max_sport_pct = (max_sport_count / len(ledger.positions) * 100) if ledger.positions else 0
        # 已实现交易统计 + holding_time 分布
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
                drawdowns = [Decimal(t.get("max_drawdown_pct", "0")) for t in realized if t.get("max_drawdown_pct")]
                if drawdowns:
                    avg_drawdown_pct = float(sum(drawdowns) / len(drawdowns))
            except Exception:
                pass
        realized_summary = {
            "trades_count": len(realized),
            "avg_holding_seconds": avg_holding,
            "max_holding_seconds": max_holding,
            "avg_max_drawdown_pct": avg_drawdown_pct,
            "recent_trades": realized[-10:],
        }
        return {
            "paper_trading_mode": True,
            "initial_budget_usdc": str(self.runtime.settings.portfolio_budget_usdc),
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
                "total_pnl_usdc": str((equity - self.runtime.settings.portfolio_budget_usdc).quantize(Decimal("0.01"))),
                "total_equity_usdc": str(equity.quantize(Decimal("0.01"))),
                "fees_accrued_usdc": str(ledger.fees_accrued_usdc),
                "fees_pct_of_initial": str((ledger.fees_accrued_usdc / self.runtime.settings.portfolio_budget_usdc * 100).quantize(Decimal("0.01"))) + "%",
            },
            "positions": {
                "count": len(ledger.positions),
                "by_token": per_pos,
            },
            "simulations": sim_stats,
            "ws_market": ws_metrics,
            "goal_progress": {
                "initial": "100",
                "target": "1000",
                "current_equity": str(equity.quantize(Decimal("0.01"))),
                "progress_pct": str(((equity - Decimal("100")) / Decimal("900") * 100).quantize(Decimal("0.01"))) + "%",
            },
        }

    def paper_orders_snapshot(self, limit: int = 50) -> dict[str, object] | None:
        """Paper trading 最近 N 条订单 + 撮合结果详情。

        含 sign/submit/cancel/replace 全部 phase + 每次 simulate_fill 的
        match_result（消耗档位/avg_price/unfilled）+ fee_quote → 复盘"为什么这单
        在这个时刻成/不成"完整数据。
        """
        if self.runtime is None or self.runtime.paper_ledger is None:
            return None
        execution_client = getattr(self.runtime.order_executor, "_client", None) if self.runtime.order_executor else None
        if execution_client is None or not hasattr(execution_client, "requests"):
            return {"paper_trading_mode": True, "requests": [], "simulations": [], "note": "execution_client unavailable"}
        requests_data = []
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
        simulations_data = []
        for trace_id, outcome in list(execution_client.simulations)[-limit:]:
            resp = outcome.response
            match = outcome.match_result
            fee = outcome.fee_quote
            simulations_data.append({
                "trace_id": trace_id,
                "status": resp.status.value if hasattr(resp.status, "value") else str(resp.status),
                "reason": resp.reason,
                "matched_shares": str(resp.matched_shares) if resp.matched_shares is not None else None,
                "spent_usdc": str(resp.spent_usdc) if resp.spent_usdc is not None else None,
                "remaining_shares": str(resp.remaining_shares) if resp.remaining_shares is not None else None,
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

    def paper_ledger_snapshot(self) -> dict[str, object] | None:
        """Paper trading 虚拟账本完整快照。非 paper 模式返回 None。

        含派生字段 avg_prices / unrealized_pnl_usdc：
        - avg_price[token] = cost_basis_usdc[token] / positions[token]
        - 浮盈 = Σ (position_shares × best_bid_now) - Σ cost_basis_usdc
          best_bid 拿不到时按 0 估（保守，FLOOR_BID_ONLY/DUST 视为无价值）
        """
        if self.runtime is None or self.runtime.paper_ledger is None:
            return None
        from decimal import Decimal
        ledger = self.runtime.paper_ledger
        ws = self.runtime.market_ws_worker
        positions = {tok: str(shares) for tok, shares in ledger.positions.items()}
        avg_prices: dict[str, str] = {}
        unrealized_value = Decimal("0")
        total_cost = Decimal("0")
        for tok, shares in ledger.positions.items():
            cost = ledger.cost_basis_usdc.get(tok, Decimal("0"))
            if shares > Decimal("0"):
                avg_prices[tok] = str((cost / shares).quantize(Decimal("0.0001")))
            total_cost += cost
            if ws is not None:
                ob = ws.snapshot(tok)
                if ob is not None and ob.best_bid is not None and ob.sell_actionable:
                    unrealized_value += shares * ob.best_bid
        return {
            "paper_trading_mode": True,
            "available_usdc": str(ledger.available_usdc),
            "fees_accrued_usdc": str(ledger.fees_accrued_usdc),
            "positions": positions,
            "cost_basis_usdc": {tok: str(v) for tok, v in ledger.cost_basis_usdc.items()},
            "avg_prices": avg_prices,
            "total_cost_usdc": str(total_cost),
            "unrealized_value_usdc": str(unrealized_value),
            "unrealized_pnl_usdc": str(unrealized_value - total_cost),
            "total_equity_usdc": str(ledger.available_usdc + unrealized_value),
        }

    def get_position_signals(
        self, *, condition_id: str | None = None, token_id: str | None = None
    ) -> dict[str, object]:
        """返回最近一次 decide_exit 决策的完整持仓信号快照。

        命名"持仓信号"而非"exit 决策信号"——内部 metadata 仍用 dynamic_exit_*
        前缀保持 audit/测试兼容，对外接口语义是持仓评估的多信号 breakdown：
        5 类投票（fair_value/imbalance/best_bid/goalserve/math_lock）+ 流动性
        tier + math_lock 是否支持 + fair_value 来源等。

        condition_id / token_id 都缺 → 返回全部持仓 signals 列表。
        """

        if self.runtime is None or self.runtime.trading_decision_worker is None:
            return {"items": []}
        cache = self.runtime.trading_decision_worker._token_position_signals
        items: list[dict[str, object]] = []
        for tid, signals in cache.items():
            if token_id is not None and tid != token_id:
                continue
            if condition_id is not None and signals.get("condition_id") != condition_id:
                continue
            items.append(signals)
        return {"items": items, "total": len(items)}

    def _serializer(self) -> AdminSerializer:
        return AdminSerializer(
            account_snapshot_provider=self._account_snapshot,
            registry_snapshot_provider=self._registry_snapshot,
            market_ws_snapshot=self._market_ws_snapshot,
        )

    def _runtime_view(self) -> AdminRuntimeView:
        return AdminRuntimeView(runtime=self.runtime)

    def _order_controller(self) -> AdminOrderController:
        strategy_id = self._runtime_strategy_id()
        if not strategy_id:
            raise RuntimeError("runtime extension missing strategy_id")
        return AdminOrderController(
            runtime=self.runtime,
            strategy_id=strategy_id,
            serializer=self._serializer(),
            account_snapshot=self._account_snapshot,
            resolve_market=self._resolve_market,
            trading_service=self._trading_service,
            find_open_order=self._find_open_order,
        )

    def _build_entry_plan_for_admin(
        self,
        *,
        market: Market,
        token_id: str,
        orderbook: OrderbookSnapshot,
        account: AccountSnapshot,
        trace_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        manual_confirmation: "ManualConfirmation | None" = None,
    ):
        # runtime.settings 是 main.py 启动后绑定的强字段——admin 路径不可能在
        # settings 缺失时执行。kelly_* 从策略侧 ConfiguredExtension.config 读取；
        # 策略配置是 kelly_* 的唯一真相来源，不再走框架 Settings。
        settings = self.runtime.settings
        strategy_config = self.runtime.strategy.config
        return self._trading_decision_service().build_entry_plan(
            market=market,
            orderbook=orderbook,
            # 候选投影和人工确认属于受控操作入口，不使用自动入场开关截断候选生成；
            # 仓位、挂单、余额仍显式传入，并在确认提交前继续经过 RiskManager。
            account_snapshot=None,
            token_id=token_id,
            trace_id=trace_id,
            portfolio_budget_usdc=settings.portfolio_budget_usdc,
            available_usdc=account.available_usdc,
            kelly_fraction=strategy_config.kelly_fraction,
            kelly_max_position_fraction=strategy_config.kelly_max_position_fraction,
            kelly_min_edge=strategy_config.kelly_min_edge,
            kelly_min_stake_usdc=strategy_config.kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=strategy_config.kelly_allow_round_up_to_market_min,
            kelly_round_up_max_overbet_ratio=strategy_config.kelly_round_up_max_overbet_ratio,
            positions=account.positions,
            open_orders=account.open_orders,
            metadata=metadata if metadata is not None else self._entry_metadata_for_market(market),
            manual_confirmation=manual_confirmation,
        )

    def _candidate_payload(self, market: Market, token_id: str, plan) -> dict[str, Any]:
        outcome = market.get_outcome_by_token_id(token_id)
        summary = plan.summary
        extras = dict(summary.extras) if summary is not None else {}
        execution_permission = extras.get("execution_permission") if extras else None
        strategy_action = summary.action if summary is not None else ""
        # 策略想 auto_execute 但 plan 因 framework 风控 / 资金 / 盘口被挡住时，
        # admin 展示统一标 reject，让运营能区分"策略主动拒绝"vs"被框架挡住"。
        if strategy_action == "auto_execute" and not plan.ready_to_trade:
            action_label = "reject"
            block_reason = ""
            allocation = plan.allocation
            if allocation is not None:
                block_reason = str(allocation.release_reason or allocation.reason or "")
            reason_text = block_reason or plan.reason or (summary.reason if summary is not None else "")
        else:
            action_label = strategy_action
            reason_text = (summary.reason if summary is not None else "") or plan.reason or ""
        accepted = bool(action_label and action_label != "reject")
        confirmable = (
            execution_permission == "manual_confirm"
            and action_label == "manual_confirm"
            and not (summary.manual_confirmed if summary is not None else False)
        )
        # 从 entry_metadata_store 反查该市场的直播源信号状态,让 candidate
        # 一次性带出"为什么被拒"的上游信息(数据源是否给出 signal_allowed).
        signal_allowed: bool | None = None
        live_state_age_ms: int | None = None
        live_state_source: str | None = None
        try:
            meta_store = self._entry_metadata_store()
            if meta_store is not None:
                meta_rec = next(
                    (r for r in meta_store.records() if r.condition_id == market.condition_id),
                    None,
                )
                if meta_rec is not None:
                    signal_allowed = meta_rec.live_state_signal_allowed
                    live_state_source = meta_rec.source
                    from datetime import datetime, timezone
                    if meta_rec.updated_at is not None:
                        live_state_age_ms = int(
                            (datetime.now(timezone.utc) - meta_rec.updated_at).total_seconds() * 1000
                        )
        except Exception:
            pass
        return {
            "candidate_id": f"{plan.trace_id}:{market.condition_id}:{token_id}",
            "strategy_id": self._runtime_strategy_id(),
            "trace_id": plan.trace_id,
            "condition_id": market.condition_id,
            "market_slug": market.market_slug,
            "event_slug": market.event_slug,
            "event_title": market.event_title,
            "token_id": token_id,
            "outcome": None if outcome is None else outcome.outcome,
            "ready_to_trade": plan.ready_to_trade,
            "accepted": accepted,
            "confirmable": confirmable,
            "decision_kind": None if plan.decision_kind is None else plan.decision_kind.value,
            "reason": reason_text,
            "action": action_label,
            "strategy_action": strategy_action,
            "execution_permission": execution_permission,
            "label": summary.label if summary is not None else "",
            "market_type": summary.market_type if summary is not None else "",
            "side": summary.side if summary is not None else "",
            "line": (
                decimal_text(summary.line) if summary is not None and summary.line is not None else None
            ),
            "best_ask": (
                decimal_text(summary.best_ask) if summary is not None and summary.best_ask is not None else None
            ),
            "manual_confirmed": summary.manual_confirmed if summary is not None else False,
            "confirmed_by": summary.confirmed_by if summary is not None else "",
            "confirm_reason": summary.confirm_reason if summary is not None else "",
            "extras": jsonable(extras),
            "allocation": None if plan.allocation is None else {
                "target_budget_usdc": decimal_text(plan.allocation.target_budget_usdc),
                "buy_budget_usdc": decimal_text(plan.allocation.buy_budget_usdc),
                "reason": plan.allocation.reason,
                "release_reason": plan.allocation.release_reason,
            },
            "intent": None if plan.intent is None else serialize_intent(plan.intent),
            "payload": jsonable(plan.metadata or {}),
            # 上游直播源信号状态(从 entry_metadata 反查),让 candidate 自带 "为什么没/被信号支持"
            "signal_allowed": signal_allowed,
            "live_state_age_ms": live_state_age_ms,
            "live_state_source": live_state_source,
        }

    def _runtime_strategy_id(self) -> str | None:
        """读取当前策略 id，供候选过滤等内存视图使用。"""

        if self.runtime is None:
            return None
        from polymarket_trader.quant.identity import STRATEGY_ID
        return STRATEGY_ID

    def _entry_metadata_for_market(self, market: Market) -> dict[str, Any]:
        store = self._entry_metadata_store()
        if store is None:
            return {}
        return store.metadata_for(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
        )

    def _candidate_source_markets(
        self,
        *,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> tuple[Market, ...]:
        """候选展示只读取已形成运行时事实的市场，不用页面请求触发全量业务评估。

        list-all 模式 (无单查参数) 额外过滤 ``market_outside_trade_window``,
        跟 polymarket 自己的 /sports/live 同口径只取"即将开赛 30min ~ 已开赛 6h"
        窗口内的 single-game 市场——避免 entry_metadata_store 累积的已结束 stale
        records 拖累每次 /candidates 评估 (实测 956 records 中绝大多数已过窗口).
        单查模式不过滤, 人查可能需要看已结束市场.
        """

        if condition_id is not None or token_id is not None or market_slug is not None:
            market = self._resolve_market(
                condition_id=condition_id,
                token_id=token_id,
                market_slug=market_slug,
            )
            return () if market is None else (market,)

        store = self._entry_metadata_store()
        registry = self.runtime.registry if self.runtime else None
        if store is None or registry is None:
            return ()

        from datetime import datetime, timezone
        from polymarket_trader.app.market_tracking_policy import market_outside_trade_window
        now = datetime.now(timezone.utc)

        markets: dict[str, Market] = {}
        for record in store.records():
            # 候选读取两类 record：
            # 1) live_state_payload 非空——single_game 主路径（sports_live_state_worker 写入）
            # 2) metadata 含 series_state / game_odds / season_odds_snapshot——series winner /
            #    outright 类市场由专用 worker 写入 metadata，不经 live_state hook。
            _has_series_data = bool(
                record.metadata.get("series_state")
                or record.metadata.get("game_odds")
                or record.metadata.get("season_odds_snapshot")
            )
            if not record.live_state_payload and not _has_series_data:
                continue
            market = None
            if record.condition_id:
                market = registry.get_by_condition_id(record.condition_id)
            if market is None and record.market_slug:
                market = registry.get_by_slug(record.market_slug)
            if market is None and record.event_slug:
                market = registry.get_by_slug(record.event_slug)
            if market is not None:
                # 时间窗过滤: outright/futures (无 game_start_time) 返 False 保留;
                # 远期未开赛 30min+ / 早已结束 6h+ 的 single-game 直接跳过.
                if market_outside_trade_window(market, now=now):
                    continue
                markets[market.condition_id] = market
        return tuple(markets.values())

    def _project_manual_entry_result(self, review, *, snapshot: AccountSnapshot) -> None:
        account_state = self.runtime.account_state_store if self.runtime else None
        if account_state is None or review.order_result is None:
            return
        strategy_id = self._runtime_strategy_id()
        if not strategy_id:
            raise RuntimeError("runtime extension missing strategy_id")
        projector = AccountStateProjector(account_state, strategy_id=strategy_id)
        projector.apply_buy_result(review.order_result, snapshot=snapshot)
        projector.apply_result_flags(review.order_result, snapshot=snapshot)

    async def _publish_candidate_confirmation_review(self, *, market: Market, plan, review) -> None:
        event_bus = self.runtime.event_bus if self.runtime else None
        if event_bus is None or plan.intent is None:
            return
        await event_bus.publish(
            OutboxPriority.P3,
            DomainEvent(
                trace_id=plan.trace_id,
                event_type=(
                    DomainEventType.RISK_CHECK_PASSED
                    if review.risk_decision is not None and review.risk_decision.passed
                    else DomainEventType.RISK_CHECK_FAILED
                ),
                event_id=uuid4().hex,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
                condition_id=market.condition_id,
                token_id=plan.intent.token_id,
                reason="risk_decision_unavailable" if review.risk_decision is None else review.risk_decision.reason,
                payload={
                    "origin": "admin_candidate_confirm",
                    "entry_origin": TRADING_DECISION_WORKER_ORIGIN,
                    "operator": jsonable(plan.summary.confirmed_by if plan.summary is not None else ""),
                    "confirm_reason": jsonable(plan.summary.confirm_reason if plan.summary is not None else ""),
                    "allocation_plan": serialize_allocation_plan(plan),
                    "allocation": serialize_allocation(plan),
                    "plan_metadata": serialize_plan_metadata(plan),
                    "intent": serialize_intent(plan.intent),
                    "review": serialize_review(review),
                },
            ),
        )

    def _account_snapshot(self) -> AccountSnapshot:
        store = self.runtime.account_state_store if self.runtime else None
        return store.snapshot() if store is not None else AccountSnapshot()

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        registry = self.runtime.registry if self.runtime else None
        return registry.snapshot() if registry is not None else MarketRegistrySnapshot(tuple())

    def _has_db_session_factory(self) -> bool:
        return self.runtime is not None and self.runtime.db_session_factory is not None

    def _market_ws_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        worker = self.runtime.market_ws_worker if self.runtime else None
        return worker.snapshot(token_id) if worker is not None else None

    def _clob_client(self) -> Any:
        if self.runtime is None:
            raise RuntimeError("clob_client unavailable")
        return self.runtime.clob_client

    def _trading_service(self) -> TradingService:
        if self.runtime is None:
            raise RuntimeError("trading_service unavailable")
        return self.runtime.trading_service

    def _trading_decision_service(self) -> TradingDecisionService:
        if self.runtime is None:
            raise RuntimeError("trading_decision_service unavailable")
        return self.runtime.trading_decision_service

    def _entry_metadata_store(self) -> Any | None:
        return self.runtime.entry_metadata_store if self.runtime else None

    def _settings_value(self, name: str) -> Any:
        settings = self.runtime.settings if self.runtime else None
        return None if settings is None else getattr(settings, name, None)

    def _resolve_market(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Market | None:
        registry = self.runtime.registry if self.runtime else None
        if registry is not None:
            if condition_id is not None:
                market = registry.get_by_condition_id(condition_id)
                if market is not None:
                    return market
            if token_id is not None:
                market = registry.get_by_token_id(token_id)
                if market is not None:
                    return market
            if market_slug is not None:
                market = registry.get_by_slug(market_slug)
                if market is not None:
                    return market
        return None

    async def _with_repositories(self, callback: Callable[[_RepositoryGroup], Any]) -> Any:
        session_factory = self.runtime.db_session_factory if self.runtime else None
        if session_factory is None:
            raise RuntimeError("db_session_factory unavailable")
        async with session_factory() as session:
            repositories = _RepositoryGroup(
                audit=AuditEventRepository(session),
                market=MarketRepository(session),
                order=OrderRepository(session),
                fill=FillRepository(session),
                position=PositionRepository(session),
                allocation=AllocationRepository(session),
                decision=DecisionRecordRepository(session),
                outbox=OutboxEventRepository(session),
                orderbook=OrderbookSnapshotRepository(session),
            )
            return await callback(repositories)

    def _slice_sequence(
        self,
        items: Sequence[Any],
        *,
        limit: int,
        offset: int,
    ) -> RepositoryPage[Any]:
        if limit <= 0:
            limit = 100
        if offset < 0:
            offset = 0
        sliced = tuple(items[offset : offset + limit])
        return RepositoryPage(items=sliced, total=len(items), limit=limit, offset=offset)

    def _find_open_order(
        self,
        snapshot: AccountSnapshot,
        *,
        order_id: str,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Order | None:
        matches = [
            order
            for order in snapshot.open_orders
            if order.open
            and order_id in {normalize_order_id(order), order.order_id, order.idempotency_key}
            and (market_slug is None or order.market_slug == market_slug)
            and (condition_id is None or order.condition_id == condition_id)
            and (token_id is None or order.token_id == token_id)
        ]
        if len(matches) != 1:
            return None
        return matches[0]

