from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import logging
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents
    from polymarket_trader.workers.market_ws.worker import MarketWsWorker
    from polymarket_trader.workers.sports_live_state_worker import SportsLiveStateWorker

from polymarket_trader.app.admin_serialization import AdminSerializer, decimal_text, jsonable
from polymarket_trader.config import Settings
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.sports_live import SportsLiveSyncStatus
from polymarket_trader.infra.polymarket.clob_client import ClobClient
from polymarket_trader.infra.polymarket.data_client import DataClient
from polymarket_trader.infra.polymarket.gamma_client import GammaClient
from polymarket_trader.runtime.event_bus import QueueDepthSnapshot
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.runtime.status import RuntimeSnapshot
from polymarket_trader.runtime.supervisor import Supervisor
from polymarket_trader.serialization import utc_now
from polymarket_trader.quant.config import CurrentStrategyConfig

logger = logging.getLogger(__name__)
_RUNTIME_MARKET_SAMPLE_LIMIT = 20
_LOW_ENTRY_FUNDS_WARNING = "available_usdc_below_configured_order_size"
# admin runtime view 暴露 sports_live_sync.recent_match_sources 的限额；超过则 truncated=True。
_SPORTS_LIVE_RECENT_MATCH_SOURCE_LIMIT = 50


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


# module-level identity cache (AdminRuntimeView 是 frozen dataclass,无法 setattr).
# key 是 id(runtime), value 是 (result_dict, timestamp).
_IDENTITY_CACHE: dict[int, tuple[dict[str, Any], float]] = {}


@dataclass(frozen=True, slots=True)
class AdminRuntimeView:
    runtime: RuntimeComponents | None = None

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "timestamp": utc_now().isoformat(),
        }

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

    async def runtime_snapshot(self) -> dict[str, Any]:
        import time as _time
        def _t(step: str, t0: float) -> None:
            try:
                from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
                SystemPerfMonitor.get().record_endpoint_step("runtime", step, (_time.perf_counter() - t0) * 1000)
            except Exception: pass
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
        bootstrap_summary = jsonable(self.runtime.bootstrap_summary if self.runtime else {}); _t("bootstrap_summary", t); t = _time.perf_counter()
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

    def _supervisor_snapshot(self) -> dict[str, Any]:
        supervisor: Supervisor | None = self.runtime.supervisor if self.runtime else None
        if supervisor is None:
            return {}
        value = supervisor.snapshot()
        # 生产路径返回 RuntimeSnapshot dataclass；少数 test stub 可能返裸 mapping，
        # 后者用 jsonable 兜底——isinstance narrow 替代 hasattr 反射。
        payload = value.as_dict() if isinstance(value, RuntimeSnapshot) else jsonable(value)
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _readiness_payload(self, supervisor: Mapping[str, Any]) -> dict[str, Any]:
        # 生产路径下 supervisor.snapshot() 总会带结构化 readiness（见 Supervisor._snapshot_with）。
        # 这里只有在 runtime.supervisor 缺席（如部分 unit test 用 SimpleNamespace 构造的 runtime，
        # 或 bootstrap 极早期 supervisor 尚未挂上）才会走 fallback。
        readiness = supervisor.get("readiness")
        if isinstance(readiness, Mapping):
            return dict(readiness)
        return self._fallback_readiness_payload()

    def _fallback_readiness_payload(self) -> dict[str, Any]:
        # 仅在 supervisor 不可用时使用（见 _readiness_payload 注释）。
        # 不要在生产链路新增依赖此分支的调用——任何新调用方应保证 supervisor.snapshot() 可用。
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
            "portfolio_budget_usdc": decimal_text((s_ := self._settings()) and isinstance(s_, Settings) and s_.portfolio_budget_usdc),
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
            {
                "field": "runtime",
                "code": str(reason),
                "message": str(reason),
            }
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
        """Kelly 框架下"账户余额低于 floor 就告警"的提示阈值。

        Kelly 没有"单笔下单上限"概念；用 ``kelly_min_stake_usdc`` 或
        ``portfolio_budget_usdc × kelly_max_position_fraction`` 中较小者作为告警下限——
        低于此值时几乎所有 Kelly 候选都会因 stake_below_min 被拒。
        """

        settings = self._settings()
        if not isinstance(settings, Settings):
            return None
        budget: Decimal | None = settings.portfolio_budget_usdc
        extension = self.runtime.extension if self.runtime else None
        kelly = extension.config if extension is not None else CurrentStrategyConfig()
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
        settings = self.runtime.readiness if self.runtime else None
        if settings is not None:
            return settings.as_dict()
        return {
            "ready_to_trade": False,
            "blocking_issues": [],
            "warnings": [],
        }

    def _settings_snapshot(self) -> dict[str, Any]:
        settings = self._settings()
        if settings is None:
            return {}
        # 生产路径 settings 是 polymarket_trader.config.Settings（含 sanitized_dump
        # 脱敏 secret 字段）。test stub 可能传 SimpleNamespace → 走 jsonable 兜底。
        if isinstance(settings, Settings):
            return settings.sanitized_dump()
        return jsonable(settings)

    async def _identity_snapshot(self) -> dict[str, Any]:
        # 60s TTL cache(module-level,因 AdminRuntimeView 是 frozen dataclass):
        # identity 含 gamma public profile HTTP ~700ms,wallet/name/avatar 变更极低.
        import time as _time
        cache_ttl_s = 60.0
        runtime_key = id(self.runtime) if self.runtime is not None else 0
        cached_entry = _IDENTITY_CACHE.get(runtime_key)
        if cached_entry is not None:
            cached_result, cached_at = cached_entry
            if (_time.time() - cached_at) < cache_ttl_s:
                return cached_result
        settings = self._settings()
        wallet_address: str | None = None
        if self.runtime is not None:
            clob: ClobClient | None = self.runtime.clob_client
            data: DataClient | None = self.runtime.data_client
            for client in (clob, data):
                if client is not None:
                    candidate = client.default_wallet_address
                    if candidate:
                        wallet_address = str(candidate)
                        break
        funder_address = _text_or_none(settings.polymarket_funder_address) if isinstance(settings, Settings) else None
        profile_address = funder_address or _text_or_none(wallet_address)
        profile = await self._public_profile(profile_address)
        # profile 来自 GammaClient 外部 API，返回类型随 API 版本变化，框架层用反射读取字段属于
        # 合法 adapter 边界——GammaClient.get_public_profile 返回 Any。
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
        gamma_client: GammaClient | None = self.runtime.gamma_client if self.runtime else None
        if gamma_client is None:
            return None
        try:
            return await gamma_client.get_public_profile(address, timeout_s=2.0)
        except Exception as exc:
            logger.warning(
                "polymarket public profile lookup failed",
                extra={"address": address, "reason": str(exc)},
            )
            return None

    def _serializer(self) -> AdminSerializer:
        return AdminSerializer(
            account_snapshot_provider=self._account_snapshot,
            registry_snapshot_provider=self._registry_snapshot,
            market_ws_snapshot=self._market_ws_snapshot,
        )

    def _account_snapshot(self) -> AccountSnapshot:
        account_state = self.runtime.account_state_store if self.runtime else None
        if account_state is not None:
            return account_state.snapshot()
        return AccountSnapshot()

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        registry = self.runtime.registry if self.runtime else None
        if registry is not None:
            return registry.snapshot()
        return MarketRegistrySnapshot(tuple())

    def _event_bus_snapshot(self) -> QueueDepthSnapshot | None:
        event_bus = self.runtime.event_bus if self.runtime else None
        if event_bus is None:
            return None
        return event_bus.snapshot()

    def _persistence_snapshot(self) -> Any | None:
        worker = self.runtime.persistence_worker if self.runtime else None
        if worker is None:
            return None
        return worker.snapshot()

    def _market_discovery_snapshot(self) -> dict[str, Any]:
        state = self.runtime.market_discovery_scan if self.runtime else None
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
        worker: SportsLiveStateWorker | None = self.runtime.sports_live_state_worker if self.runtime else None
        if worker is not None:
            status = worker.status_snapshot()
            # 生产路径返回 SportsLiveSyncStatus dataclass；test stub 走 jsonable 兜底。
            if isinstance(status, SportsLiveSyncStatus):
                snapshot = status.as_dict()
            else:
                payload = jsonable(status)
                snapshot = dict(payload) if isinstance(payload, Mapping) else {"value": payload}
            recent_sources = list(worker.recent_match_sources(limit=_SPORTS_LIVE_RECENT_MATCH_SOURCE_LIMIT))
            # 后端持续暴露 truncated 标记，便于前端展示"还有更早匹配未展示"。
            full_count = len(worker.recent_match_sources(limit=None))
            snapshot["recent_match_sources"] = recent_sources
            snapshot["recent_match_sources_limit"] = _SPORTS_LIVE_RECENT_MATCH_SOURCE_LIMIT
            snapshot["recent_match_sources_truncated"] = full_count > _SPORTS_LIVE_RECENT_MATCH_SOURCE_LIMIT
            # Per-sport WS / HTTP 连接状态（WS 连接是否稳定、每个 sport HTTP 轮询是否健康）。
            client = self.runtime.sports_live_state_client if self.runtime else None
            snapshot["source_detail"] = client.source_detail_status() if client is not None else []
            return snapshot
        settings = self._settings()
        live_enabled = settings.sports_live_state_enabled if isinstance(settings, Settings) else False
        return {
            "enabled": live_enabled,
            "source": "sports_live_aggregate",
            "running": False,
            "last_started_at": None,
            "last_completed_at": None,
            "last_success_at": None,
            "last_error": "sports_live_state_worker_unavailable" if live_enabled else None,
            "consecutive_failures": 0,
            "last_events_seen": 0,
            "last_markets_seen": 0,
            "last_matches": 0,
            "last_records_written": 0,
            "last_unmatched_markets": 0,
            "last_entry_signals_published": 0,
            "leagues": list(settings.sports_live_state_league_codes if isinstance(settings, Settings) else ()),
            "source_statuses": [],
            "recent_match_sources": [],
            "recent_match_sources_limit": _SPORTS_LIVE_RECENT_MATCH_SOURCE_LIMIT,
            "recent_match_sources_truncated": False,
        }

    def _market_ws_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        worker: MarketWsWorker | None = self.runtime.market_ws_worker if self.runtime else None
        if worker is None:
            return None
        return worker.snapshot(token_id)

    def _settings(self) -> Settings | None:
        return self.runtime.settings if self.runtime else None
