"""RuntimeAggregator —— runtime/health/readiness/workers/metrics + portfolio
snapshot + paper ledger + risk metrics + data freshness + outbox queue depth。

原架构方案 §12.2 ① 运营查询类（走 runtime 内存快照 + 短 TTL 缓存）。

paper trading 量化诊断 / 异常检测 / CLV 等专题在 `PaperTradingAggregator`；
系统性能 / 内存 / 数据源健康在 `SystemObservabilityAggregator`——本 aggregator
只负责 operator 主仪表盘的高频核心视图。
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from statistics import mean, stdev
from typing import TYPE_CHECKING, Any, Mapping

from polymarket_trader.serialization import decimal_text, jsonable
from polymarket_trader.api.serialization import ApiSerializer
from polymarket_trader.config import Settings
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.infra.polymarket.clob_client import ClobClient
from polymarket_trader.infra.polymarket.data_client import DataClient
from polymarket_trader.infra.polymarket.gamma_client import GammaClient
from polymarket_trader.runtime.event_bus import QueueDepthSnapshot
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.runtime.status import RuntimeSnapshot
from polymarket_trader.runtime.supervisor import Supervisor
from polymarket_trader.serialization import utc_now
from polymarket_trader.workflow.config import TradingWorkflowConfig

from ._db import RepositoryGroup, with_repositories

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from polymarket_trader.main import RuntimeComponents
    from polymarket_trader.pipeline.ingest.orderbook_ws.worker import MarketWsWorker

logger = logging.getLogger(__name__)

_RUNTIME_MARKET_SAMPLE_LIMIT = 20
_LOW_ENTRY_FUNDS_WARNING = "available_usdc_below_configured_order_size"

# identity 60s TTL cache（含 gamma public profile HTTP ~700ms）
_IDENTITY_CACHE: dict[int, tuple[dict[str, Any], float]] = {}


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _warning_key(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("code") or value)
    return str(value)


def _dedupe_warnings(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = _warning_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


class RuntimeAggregator:
    def __init__(
        self,
        *,
        runtime: "RuntimeComponents | None" = None,
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
    ) -> None:
        self._runtime = runtime
        self._session_factory = session_factory

    # ---------- health / readiness ----------
    def health_snapshot(self) -> dict[str, Any]:
        return {"status": "ok", "timestamp": utc_now().isoformat()}

    def readiness_snapshot(self) -> dict[str, Any]:
        supervisor = self._supervisor_snapshot()
        readiness = self._readiness_payload(supervisor)
        config_readiness = self._config_readiness_snapshot()
        return {
            "ready_to_trade": bool(readiness.get("ready")),
            "phase": self._phase_text(supervisor, readiness),
            "blocking_issues": self._blocking_issues(config_readiness, readiness),
            "blocking_reasons": list(readiness.get("blocking_reasons", ())),
            "warnings": self._readiness_warnings(config_readiness, readiness),
            "runtime": self._runtime_status_snapshot(supervisor, readiness),
        }

    # ---------- runtime snapshot ----------
    async def runtime_snapshot(self) -> dict[str, Any]:
        def _t(step: str, t0: float) -> None:
            try:
                from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
                SystemPerfMonitor.get().record_endpoint_step(
                    "runtime", step, (_time.perf_counter() - t0) * 1000
                )
            except Exception:  # noqa: BLE001
                pass

        t = _time.perf_counter()
        supervisor = self._supervisor_snapshot(); _t("supervisor", t); t = _time.perf_counter()
        readiness = self._readiness_payload(supervisor); _t("readiness", t); t = _time.perf_counter()
        config_readiness = self._config_readiness_snapshot(); _t("config_readiness", t); t = _time.perf_counter()
        readiness_view = dict(readiness)
        readiness_view["warnings"] = tuple(self._readiness_warnings(config_readiness, readiness))
        runtime_status = self._runtime_status_snapshot(supervisor, readiness)
        account = self._account_snapshot(); _t("account", t); t = _time.perf_counter()
        registry = self._registry_snapshot(); _t("registry", t); t = _time.perf_counter()
        market_sample = [
            self._serializer().market_view(market)
            for market in registry.markets[:_RUNTIME_MARKET_SAMPLE_LIMIT]
        ]; _t("market_sample_view", t); t = _time.perf_counter()
        markets_truncated = len(registry.markets) > _RUNTIME_MARKET_SAMPLE_LIMIT
        identity = await self._identity_snapshot(); _t("identity_async", t); t = _time.perf_counter()
        settings_payload = self._settings_snapshot(); _t("settings", t); t = _time.perf_counter()
        bootstrap_summary = jsonable(self._runtime.bootstrap_summary if self._runtime else {}); _t("bootstrap_summary", t); t = _time.perf_counter()
        market_discovery = self._market_discovery_snapshot(); _t("market_discovery", t); t = _time.perf_counter()
        sports_live_sync = self._sports_live_sync_snapshot(); _t("sports_live_sync", t); t = _time.perf_counter()
        market_sample_jsonable = [jsonable(market) for market in market_sample]; _t("market_sample_jsonable", t); t = _time.perf_counter()
        account_serialized = self._serializer().account_snapshot(account); _t("account_serialize", t); t = _time.perf_counter()
        event_bus_payload = jsonable(self._event_bus_snapshot()); _t("event_bus_snapshot", t); t = _time.perf_counter()
        persistence_payload = jsonable(self._persistence_snapshot()); _t("persistence_snapshot", t); t = _time.perf_counter()
        portfolio_payload = self._serializer().portfolio_snapshot(account); _t("portfolio_snapshot", t)
        return {
            "phase": runtime_status["phase"],
            "ready_to_trade": runtime_status["ready_to_trade"],
            "readiness": readiness_view,
            "settings": settings_payload,
            "identity": identity,
            "runtime": runtime_status,
            "bootstrap_summary": bootstrap_summary,
            "market_discovery": market_discovery,
            "sports_live_sync": sports_live_sync,
            "registry": {
                "market_count": len(registry.markets),
                "market_sample": market_sample_jsonable,
                "market_sample_limit": _RUNTIME_MARKET_SAMPLE_LIMIT,
                "markets_truncated": markets_truncated,
            },
            "account": account_serialized,
            "event_bus": event_bus_payload,
            "persistence": persistence_payload,
            "market_sample": market_sample,
            "portfolio": portfolio_payload,
        }

    def workers_snapshot(self) -> dict[str, Any]:
        supervisor = self._supervisor_snapshot()
        runtime_status = self._runtime_status_snapshot(supervisor, self._readiness_payload(supervisor))
        return {
            "phase": runtime_status["phase"],
            "automatic_trading_enabled": bool(supervisor.get("automatic_trading_enabled")),
            "queue_depths": supervisor.get("queue_depths"),
            "scheduler": supervisor.get("scheduler"),
            "sports_live_sync": self._sports_live_sync_snapshot(),
            "workers": list(supervisor.get("worker_health", ())),
        }

    def metrics_snapshot(self) -> dict[str, Any]:
        supervisor = self._supervisor_snapshot()
        runtime_status = self._runtime_status_snapshot(supervisor, self._readiness_payload(supervisor))
        return {
            "phase": runtime_status["phase"],
            "automatic_trading_enabled": bool(supervisor.get("automatic_trading_enabled")),
            "queue_depths": supervisor.get("queue_depths"),
            "metrics": supervisor.get("metrics"),
            "sports_live_sync": self._sports_live_sync_snapshot(),
            "sse_active_subscribers": supervisor.get("sse_active_subscribers", 0),
            "sse_dropped_events_total": supervisor.get("sse_dropped_events_total", 0),
        }

    # ---------- portfolio snapshot ----------
    async def portfolio_snapshot(self) -> dict[str, Any]:
        account = self._account_snapshot()
        base = self._portfolio_pnl_aggregates(account)

        if self._session_factory is None:
            return {**base, "recent_allocations": []}

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.allocation.list_allocations_snapshot(limit=50, offset=0)

        allocations = await with_repositories(self._session_factory, _query)
        return {
            **base,
            "recent_allocations": [
                self._serializer().allocation(allocation) for allocation in allocations.items
            ],
        }

    def _portfolio_pnl_aggregates(self, account: Any) -> dict[str, Any]:
        positions = account.positions
        zero = Decimal("0")
        total_cost = sum((p.cost_usdc for p in positions), zero)
        total_current_value = sum((p.current_value or zero for p in positions), zero)
        total_cash_pnl = sum((p.cash_pnl or zero for p in positions), zero)
        total_realized_pnl = sum((p.realized_pnl or zero for p in positions), zero)
        open_position_count = sum(1 for p in positions if p.shares > zero)
        net_value_usdc = account.balance_usdc + total_current_value
        registry = self._registry_snapshot()
        return {
            "balance_usdc": decimal_text(account.balance_usdc),
            "allowance_usdc": decimal_text(account.allowance_usdc),
            "available_usdc": decimal_text(account.available_usdc),
            "net_value_usdc": decimal_text(net_value_usdc),
            "notional_usdc": decimal_text(total_current_value),
            "cost_usdc": decimal_text(total_cost),
            "cash_pnl_usdc": decimal_text(total_cash_pnl),
            "realized_pnl_usdc": decimal_text(total_realized_pnl),
            "position_count": len(positions),
            "open_position_count": open_position_count,
            "open_order_count": len(account.open_orders),
            "fill_count": len(account.fills),
            "pause_count": len(account.market_pauses),
            "last_reconcile_at": jsonable(account.last_reconcile_at),
            "user_ws_connected": account.user_ws_connected,
            "allow_new_entries": account.allow_new_entries,
            "markets_tracked": len(registry.markets),
        }

    # ---------- outbox queue depth ----------
    def outbox_queue_depth(self) -> dict[str, Any]:
        event_bus = self._runtime.event_bus if self._runtime else None
        if event_bus is None:
            return {"available": False}

        snap = event_bus.snapshot()

        def _pct(depth: int, cap: int) -> str | None:
            if cap <= 0:
                return None
            return decimal_text(Decimal(str(round(depth / cap * 100, 1))))

        return {
            "available": True,
            "trading": {
                "depth": snap.trading_queue_depth,
                "capacity": snap.trading_queue_capacity,
                "retained": snap.trading_retained_depth,
                "utilization_pct": _pct(snap.trading_queue_depth, snap.trading_queue_capacity),
            },
            "maintenance": {
                "depth": snap.maintenance_queue_depth,
                "capacity": snap.maintenance_queue_capacity,
                "retained": snap.maintenance_retained_depth,
                "utilization_pct": _pct(snap.maintenance_queue_depth, snap.maintenance_queue_capacity),
            },
            "persistence": {
                "depth": snap.persistence_queue_depth,
                "capacity": snap.persistence_queue_capacity,
                "retained": snap.persistence_retained_depth,
                "utilization_pct": _pct(snap.persistence_queue_depth, snap.persistence_queue_capacity),
            },
            "low_priority_paused": snap.low_priority_paused,
        }

    # ---------- data freshness ----------
    def data_freshness(self) -> dict[str, Any]:
        store = self._entry_metadata_store()
        if store is None:
            return {"available": False, "items": []}

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        items = []
        for record in store.records():
            updated_at = record.updated_at
            age_ms: int | None = None
            try:
                age_ms = now_ms - int(updated_at.timestamp() * 1000)
            except Exception:  # noqa: BLE001
                pass
            items.append({
                "condition_id": record.condition_id,
                "market_slug": record.market_slug,
                "event_slug": record.event_slug,
                "source": record.source,
                "has_live_state": bool(record.live_state_payload),
                "signal_allowed": record.live_state_signal_allowed,
                "staleness_ms": age_ms,
                "updated_at": jsonable(updated_at),
            })
        items.sort(key=lambda x: x["staleness_ms"] if x["staleness_ms"] is not None else -1, reverse=True)
        return {"available": True, "item_count": len(items), "items": items}

    # ---------- paper ledger ----------
    def paper_ledger_snapshot(self) -> dict[str, object] | None:
        if self._runtime is None or self._runtime.paper_ledger is None:
            return None
        ledger = self._runtime.paper_ledger
        ws = self._runtime.market_ws_worker
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

    # ---------- risk metrics (Sharpe / Sortino / Calmar / VaR) ----------
    def risk_metrics_snapshot(self) -> dict[str, object] | None:
        if self._runtime is None or self._runtime.paper_ledger is None:
            return None
        curve = getattr(self._runtime.paper_ledger, "equity_curve", [])
        if len(curve) < 5:
            return {"error": "需要至少 5 个数据点（每分钟 1 点）", "points_count": len(curve)}
        equities = [float(p["equity_usdc"]) for p in curve]
        returns = [
            (equities[i] - equities[i - 1]) / equities[i - 1]
            for i in range(1, len(equities))
            if equities[i - 1] > 0
        ]
        if not returns:
            return {"error": "no valid returns", "points_count": len(curve)}
        mean_ret = mean(returns)
        std_ret = stdev(returns) if len(returns) >= 2 else 0
        periods_per_year = 525600
        sharpe = (mean_ret / std_ret * (periods_per_year ** 0.5)) if std_ret > 0 else None
        downside = [r for r in returns if r < 0]
        downside_std = stdev(downside) if len(downside) >= 2 else 0
        sortino = (mean_ret / downside_std * (periods_per_year ** 0.5)) if downside_std > 0 else None
        peak = equities[0]
        max_dd = 0.0
        for eq in equities:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
        total_return = (equities[-1] - equities[0]) / equities[0] if equities[0] > 0 else 0
        period_minutes = len(curve)
        annualized_return = total_return * (periods_per_year / period_minutes) if period_minutes > 0 else 0
        calmar = (annualized_return / max_dd) if max_dd > 0 else None
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

    # ===================== internal helpers =====================

    def _supervisor_snapshot(self) -> dict[str, Any]:
        supervisor: Supervisor | None = self._runtime.supervisor if self._runtime else None
        if supervisor is None:
            return {}
        value = supervisor.snapshot()
        payload = value.as_dict() if isinstance(value, RuntimeSnapshot) else jsonable(value)
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _readiness_payload(self, supervisor: Mapping[str, Any]) -> dict[str, Any]:
        readiness = supervisor.get("readiness")
        if isinstance(readiness, Mapping):
            return dict(readiness)
        return self._fallback_readiness_payload()

    def _fallback_readiness_payload(self) -> dict[str, Any]:
        config = self._config_readiness_snapshot()
        account = self._account_snapshot()
        reasons: list[str] = []
        if not bool(config.get("ready_to_trade")):
            reasons.append("config_not_ready")
        if not account.user_ws_connected:
            reasons.append("user_ws_not_connected")
        if account.last_reconcile_at is None:
            reasons.append("reconcile_not_fresh")
        if not account.allow_new_entries:
            reasons.append("allow_new_entries_closed")
        return {
            "phase": "trading_enabled" if not reasons else "recovering_snapshot",
            "live": True,
            "ready": not reasons,
            "automatic_trading_enabled": not reasons,
            "config_ready": bool(config.get("ready_to_trade")),
            "user_ws_connected": account.user_ws_connected,
            "reconcile_fresh": account.last_reconcile_at is not None,
            "low_priority_paused": False,
            "blocking_reasons": tuple(reasons),
            "warnings": tuple(config.get("warnings", ())),
            "last_reconcile_at": account.last_reconcile_at,
        }

    def _runtime_status_snapshot(
        self,
        supervisor: Mapping[str, Any],
        readiness: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw_account = supervisor.get("account")
        raw_user_ws = supervisor.get("user_ws")
        account_payload: Mapping[str, Any] = raw_account if isinstance(raw_account, Mapping) else {}
        user_ws: Mapping[str, Any] = raw_user_ws if isinstance(raw_user_ws, Mapping) else {}
        fallback_account = self._account_snapshot() if not account_payload else None
        fallback_user_ws_connected = False if fallback_account is None else fallback_account.user_ws_connected
        fallback_allow_entries = True if fallback_account is None else fallback_account.allow_new_entries
        fallback_reconcile_at = None if fallback_account is None else fallback_account.last_reconcile_at
        settings = self._settings()
        return {
            "phase": self._phase_text(supervisor, readiness),
            "ready_to_trade": bool(readiness.get("ready")),
            "automatic_trading_enabled": bool(supervisor.get("automatic_trading_enabled", readiness.get("ready"))),
            "user_ws_connected": bool(
                readiness.get(
                    "user_ws_connected",
                    user_ws.get("connected", account_payload.get("user_ws_connected", fallback_user_ws_connected)),
                )
            ),
            "allow_new_entries": bool(account_payload.get("allow_new_entries", fallback_allow_entries)),
            "last_reconcile_at": readiness.get("last_reconcile_at")
            or account_payload.get("last_reconcile_at")
            or fallback_reconcile_at,
            "portfolio_budget_usdc": decimal_text(
                settings.portfolio_budget_usdc if isinstance(settings, Settings) else None
            ),
            "queue_depth": supervisor.get("queue_depths") or jsonable(self._event_bus_snapshot()),
            "persistence": supervisor.get("persistence") or jsonable(self._persistence_snapshot()),
            "blocking_reasons": tuple(readiness.get("blocking_reasons", ())),
            "warnings": tuple(self._readiness_warnings(self._config_readiness_snapshot(), readiness)),
        }

    def _phase_text(self, supervisor: Mapping[str, Any], readiness: Mapping[str, Any]) -> str:
        phase = supervisor.get("phase") or readiness.get("phase") or "starting"
        return str(phase)

    def _blocking_issues(
        self,
        config_readiness: Mapping[str, Any],
        readiness: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        config_issues = config_readiness.get("blocking_issues", ())
        if config_issues:
            return [jsonable(issue) for issue in config_issues if isinstance(issue, Mapping)]
        return [
            {"field": "runtime", "code": str(reason), "message": str(reason)}
            for reason in readiness.get("blocking_reasons", ())
        ]

    def _readiness_warnings(
        self,
        config_readiness: Mapping[str, Any],
        readiness: Mapping[str, Any],
    ) -> list[Any]:
        warnings = list(readiness.get("warnings") or config_readiness.get("warnings", ()))
        warnings.extend(self._funding_warnings())
        return _dedupe_warnings(warnings)

    def _funding_warnings(self) -> tuple[str, ...]:
        account = self._account_snapshot()
        floor = self._configured_entry_floor_usdc()
        if floor is None or floor <= Decimal("0"):
            return ()
        if account.available_usdc < floor:
            return (_LOW_ENTRY_FUNDS_WARNING,)
        return ()

    def _configured_entry_floor_usdc(self) -> Decimal | None:
        settings = self._settings()
        if not isinstance(settings, Settings):
            return None
        budget: Decimal | None = settings.portfolio_budget_usdc
        strategy = self._runtime.workflow if self._runtime else None
        kelly = strategy.config if strategy is not None else TradingWorkflowConfig()
        max_position_fraction = kelly.kelly_max_position_fraction
        min_stake = kelly.kelly_min_stake_usdc
        candidates: list[Decimal] = []
        if budget is not None and max_position_fraction is not None and max_position_fraction > 0:
            position_floor = budget * max_position_fraction
            if position_floor > Decimal("0"):
                candidates.append(position_floor)
        if min_stake is not None and min_stake > Decimal("0"):
            candidates.append(min_stake)
        if not candidates:
            return None
        return min(candidates)

    def _config_readiness_snapshot(self) -> dict[str, Any]:
        settings = self._runtime.readiness if self._runtime else None
        if settings is not None:
            return settings.as_dict()
        return {"ready_to_trade": False, "blocking_issues": [], "warnings": []}

    def _settings_snapshot(self) -> dict[str, Any]:
        settings = self._settings()
        if settings is None:
            return {}
        if isinstance(settings, Settings):
            return settings.sanitized_dump()
        return jsonable(settings)

    async def _identity_snapshot(self) -> dict[str, Any]:
        cache_ttl_s = 60.0
        runtime_key = id(self._runtime) if self._runtime is not None else 0
        cached_entry = _IDENTITY_CACHE.get(runtime_key)
        if cached_entry is not None:
            cached_result, cached_at = cached_entry
            if (_time.time() - cached_at) < cache_ttl_s:
                return cached_result
        settings = self._settings()
        wallet_address: str | None = None
        if self._runtime is not None:
            clob: ClobClient | None = self._runtime.clob_client
            data: DataClient | None = self._runtime.data_client
            for client in (clob, data):
                if client is not None:
                    candidate = client.default_wallet_address
                    if candidate:
                        wallet_address = str(candidate)
                        break
        funder_address = (
            _text_or_none(settings.polymarket_funder_address)
            if isinstance(settings, Settings)
            else None
        )
        profile_address = funder_address or _text_or_none(wallet_address)
        profile = await self._public_profile(profile_address)
        display_username_public = (
            None if profile is None else getattr(profile, "display_username_public", None)
        )
        profile_name = None
        if display_username_public is not False:
            profile_name = _text_or_none(None if profile is None else getattr(profile, "name", None))
        result = {
            "wallet_address": wallet_address,
            "funder_address": funder_address,
            "signature_type": settings.polymarket_signature_type if isinstance(settings, Settings) else None,
            "profile_address": (
                _text_or_none(None if profile is None else getattr(profile, "proxy_wallet", None))
                or profile_address
            ),
            "profile_name": profile_name,
            "profile_pseudonym": _text_or_none(
                None if profile is None else getattr(profile, "pseudonym", None)
            ),
            "profile_image": _text_or_none(
                None if profile is None else getattr(profile, "profile_image", None)
            ),
            "profile_verified": (
                None if profile is None else getattr(profile, "verified_badge", None)
            ),
            "profile_x_username": _text_or_none(
                None if profile is None else getattr(profile, "x_username", None)
            ),
        }
        _IDENTITY_CACHE[runtime_key] = (result, _time.time())
        return result

    async def _public_profile(self, address: str | None) -> Any | None:
        if address is None:
            return None
        gamma_client: GammaClient | None = self._runtime.gamma_client if self._runtime else None
        if gamma_client is None:
            return None
        try:
            return await gamma_client.get_public_profile(address, timeout_s=2.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "polymarket public profile lookup failed",
                extra={"address": address, "reason": str(exc)},
            )
            return None

    def _serializer(self) -> ApiSerializer:
        return ApiSerializer(
            account_snapshot_provider=self._account_snapshot,
            registry_snapshot_provider=self._registry_snapshot,
            market_ws_snapshot=self._market_ws_snapshot,
        )

    def _account_snapshot(self) -> AccountSnapshot:
        account_state = self._runtime.account_state_store if self._runtime else None
        if account_state is not None:
            return account_state.snapshot()
        return AccountSnapshot()

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        registry = self._runtime.registry if self._runtime else None
        if registry is not None:
            return registry.snapshot()
        return MarketRegistrySnapshot(tuple())

    def _event_bus_snapshot(self) -> QueueDepthSnapshot | None:
        event_bus = self._runtime.event_bus if self._runtime else None
        if event_bus is None:
            return None
        return event_bus.snapshot()

    def _persistence_snapshot(self) -> Any | None:
        worker = self._runtime.persistence_worker if self._runtime else None
        if worker is None:
            return None
        return worker.snapshot()

    def _market_discovery_snapshot(self) -> dict[str, Any]:
        state = self._runtime.market_discovery_scan if self._runtime else None
        if state is None:
            return {
                "round_id": 0,
                "cursor_active": False,
                "round_started_at": None,
                "last_round_completed_at": None,
                "pages_scanned_in_round": 0,
                "markets_seen_in_round": 0,
                "last_completed_round_pages": 0,
                "last_completed_round_markets": 0,
                "last_page_size": 0,
                "last_tick_started_at": None,
                "last_tick_completed_at": None,
                "last_tick_requests": 0,
                "last_tick_markets": 0,
                "last_error": None,
                "consecutive_failures": 0,
            }
        return {
            "round_id": state.round_id,
            "cursor_active": state.after_cursor is not None or bool(state.query_cursors),
            "query_cursors": jsonable(state.query_cursors),
            "completed_query_names": sorted(str(name) for name in state.completed_query_names),
            "active_cursor_count": len(state.query_cursors),
            "completed_query_count": len(state.completed_query_names),
            "round_started_at": jsonable(state.round_started_at),
            "last_round_completed_at": jsonable(state.last_round_completed_at),
            "pages_scanned_in_round": state.pages_scanned_in_round,
            "markets_seen_in_round": state.markets_seen_in_round,
            "last_completed_round_pages": state.last_completed_round_pages,
            "last_completed_round_markets": state.last_completed_round_markets,
            "last_page_size": state.last_page_size,
            "last_tick_started_at": jsonable(state.last_tick_started_at),
            "last_tick_completed_at": jsonable(state.last_tick_completed_at),
            "last_tick_requests": state.last_tick_requests,
            "last_tick_markets": state.last_tick_markets,
            "last_error": state.last_error,
            "consecutive_failures": state.consecutive_failures,
        }

    def _sports_live_sync_snapshot(self) -> dict[str, Any]:
        settings = self._settings()
        live_enabled = settings.sports_live_state_enabled if isinstance(settings, Settings) else False
        if self._runtime is None:
            return {"enabled": live_enabled, "buckets": [], "subscriptions": {}}
        store = self._runtime.live_state_store
        registry = self._runtime.live_source_registry
        buckets = []
        for bucket in store.all_buckets():
            buckets.append(
                {
                    "source": bucket.source.as_label(),
                    "provider": bucket.source.provider.value,
                    "sport": bucket.source.sport,
                    "events_count": len(bucket.events),
                    "observed_at": bucket.observed_at.isoformat(),
                    "health": bucket.health.value,
                    "last_error": bucket.last_error,
                    "subscriber_count": len(registry.subscribers_for(bucket.source)),
                }
            )
        return {"enabled": live_enabled, "buckets": buckets, "subscriptions": registry.summary()}

    def _market_ws_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        worker: MarketWsWorker | None = self._runtime.market_ws_worker if self._runtime else None
        if worker is None:
            return None
        return worker.snapshot(token_id)

    def _entry_metadata_store(self) -> Any | None:
        return getattr(self._runtime, "market_metadata_store", None) if self._runtime else None

    def _settings(self) -> Settings | None:
        return self._runtime.settings if self._runtime else None
