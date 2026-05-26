from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents


from polymarket_trader.app.admin_order_control import AdminOrderController
from polymarket_trader.app.admin_serialization import AdminSerializer, decimal_text, jsonable
from polymarket_trader.app.admin_service_helpers import (
    _RepositoryGroup,
)
from polymarket_trader.app.order_projection import AccountStateProjector, normalize_order_id
from polymarket_trader.pipeline.decision.decision_context_builder import DecisionContextBuilder
from polymarket_trader.pipeline.execution.order_gateway import OrderGateway
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.decisions import ManualConfirmation
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


# Module-level cache for trade tape（frozen dataclass 不能含 mutable state）
_TRADE_TAPE_CACHE: dict[str, tuple[float, dict]] = {}


@dataclass(frozen=True, slots=True)
class AdminService(AdminControlsMixin):
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
        from polymarket_trader.pipeline.decision.worker import get_odds_drift_store
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

    def odds_drift_snapshot(self, market_slug: str | None = None, limit: int = 100) -> dict[str, object]:
        """Goalserve 赔率漂移时序（每 market 5s 采样 1 次，最多保留 200 点）。

        - market_slug 为空：返回所有 tracked market 的简要统计
        - 含 market_slug：返回该 market 完整时序 + 派生漂移率
        派生：
        - ml_home_p_change_5min: 最近 5min 内 home 隐含概率变化
        - vig_pct_current: 当前 ML overround
        - vig_change_5min: vig 变化（庄家收紧 / 放松信号）
        """
        from polymarket_trader.pipeline.decision.worker import get_odds_drift_store
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

        if self.runtime is None or self.runtime.market_tick_worker is None:
            return {"items": []}
        cache = self.runtime.market_tick_worker._token_position_signals
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

    def _order_controller(self) -> AdminOrderController:
        return AdminOrderController(
            runtime=self.runtime,
            serializer=self._serializer(),
            account_snapshot=self._account_snapshot,
            resolve_market=self._resolve_market,
            order_gateway=self._order_gateway,
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
        # settings 缺失时执行。kelly_* 从策略侧 TradingWorkflow.config 读取；
        # 策略配置是 kelly_* 的唯一真相来源，不再走框架 Settings。
        settings = self.runtime.settings
        strategy_config = self.runtime.workflow.config
        return self._decision_builder().build_entry_plan(
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
        # 从 market_metadata_store 反查该市场的直播源信号状态,让 candidate
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
        窗口内的 single-game 市场——避免 market_metadata_store 累积的已结束 stale
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
        projector = AccountStateProjector(account_state)
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

    def _order_gateway(self) -> OrderGateway:
        if self.runtime is None:
            raise RuntimeError("order_gateway unavailable")
        return self.runtime.order_gateway

    def _decision_builder(self) -> DecisionContextBuilder:
        if self.runtime is None:
            raise RuntimeError("decision_context_builder unavailable")
        return self.runtime.decision_context_builder

    def _entry_metadata_store(self) -> Any | None:
        return self.runtime.market_metadata_store if self.runtime else None

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

