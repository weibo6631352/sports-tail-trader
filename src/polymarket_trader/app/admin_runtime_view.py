from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Mapping

from polymarket_trader.app.admin_serialization import AdminSerializer, decimal_text, jsonable
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.runtime.event_bus import QueueDepthSnapshot
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.serialization import utc_now

logger = logging.getLogger(__name__)


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class AdminRuntimeView:
    runtime: Any | None = None

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
            "warnings": list(readiness.get("warnings") or config_readiness.get("warnings", ())),
            "runtime": self._runtime_status_snapshot(supervisor, readiness),
        }

    async def runtime_snapshot(self) -> dict[str, Any]:
        supervisor = self._supervisor_snapshot()
        readiness = self._readiness_payload(supervisor)
        runtime_status = self._runtime_status_snapshot(supervisor, readiness)
        account = self._account_snapshot()
        registry = self._registry_snapshot()
        markets = [self._serializer().market_view(market) for market in registry.markets]
        return {
            "phase": runtime_status["phase"],
            "ready_to_trade": runtime_status["ready_to_trade"],
            "readiness": readiness,
            "settings": self._settings_snapshot(),
            "identity": await self._identity_snapshot(),
            "runtime": runtime_status,
            "bootstrap_summary": jsonable(getattr(self.runtime, "bootstrap_summary", {})),
            "market_discovery": self._market_discovery_snapshot(),
            "registry": {
                "market_count": len(registry.markets),
                "markets": [jsonable(market) for market in markets],
            },
            "account": self._serializer().account_snapshot(account),
            "event_bus": jsonable(self._event_bus_snapshot()),
            "persistence": jsonable(self._persistence_snapshot()),
            "markets": markets,
            "portfolio": self._serializer().portfolio_snapshot(account),
        }

    def workers_snapshot(self) -> dict[str, Any]:
        supervisor = self._supervisor_snapshot()
        runtime_status = self._runtime_status_snapshot(supervisor, self._readiness_payload(supervisor))
        return {
            "phase": runtime_status["phase"],
            "automatic_trading_enabled": bool(supervisor.get("automatic_trading_enabled")),
            "queue_depths": supervisor.get("queue_depths"),
            "scheduler": supervisor.get("scheduler"),
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
        }

    def _supervisor_snapshot(self) -> dict[str, Any]:
        supervisor = getattr(self.runtime, "supervisor", None)
        snapshot = getattr(supervisor, "snapshot", None)
        if not callable(snapshot):
            return {}
        value = snapshot()
        payload = value.as_dict() if hasattr(value, "as_dict") else jsonable(value)
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
            "portfolio_budget_usdc": decimal_text(getattr(self._settings(), "portfolio_budget_usdc", None)),
            "queue_depth": supervisor.get("queue_depths") or jsonable(self._event_bus_snapshot()),
            "persistence": supervisor.get("persistence") or jsonable(self._persistence_snapshot()),
            "blocking_reasons": tuple(readiness.get("blocking_reasons", ())),
            "warnings": tuple(readiness.get("warnings", ())),
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

    def _config_readiness_snapshot(self) -> dict[str, Any]:
        settings = getattr(self.runtime, "readiness", None)
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
        if hasattr(settings, "sanitized_dump"):
            return settings.sanitized_dump()
        return jsonable(settings)

    async def _identity_snapshot(self) -> dict[str, Any]:
        settings = self._settings()
        wallet_address = None
        for component_name in ("clob_client", "data_client"):
            component = getattr(self.runtime, component_name, None)
            if component is None:
                continue
            candidate = getattr(component, "default_wallet_address", None)
            if callable(candidate):
                candidate = candidate()
            if candidate:
                wallet_address = str(candidate)
                break
        funder_address = (
            _text_or_none(getattr(settings, "polymarket_funder_address", None))
            if settings is not None
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
        return {
            "wallet_address": wallet_address,
            "funder_address": funder_address,
            "signature_type": (
                getattr(settings, "polymarket_signature_type", None) if settings is not None else None
            ),
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

    async def _public_profile(self, address: str | None) -> Any | None:
        if address is None:
            return None
        gamma_client = getattr(self.runtime, "gamma_client", None)
        get_public_profile = getattr(gamma_client, "get_public_profile", None)
        if not callable(get_public_profile):
            return None
        try:
            return await get_public_profile(address, timeout_s=2.0)
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
        account_state = getattr(self.runtime, "account_state_store", None)
        if account_state is not None:
            return account_state.snapshot()
        return AccountSnapshot()

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        registry = getattr(self.runtime, "registry", None)
        if registry is not None:
            return registry.snapshot()
        return MarketRegistrySnapshot(tuple())

    def _event_bus_snapshot(self) -> QueueDepthSnapshot | None:
        event_bus = getattr(self.runtime, "event_bus", None)
        if event_bus is None:
            return None
        return event_bus.snapshot()

    def _persistence_snapshot(self) -> Any | None:
        worker = getattr(self.runtime, "persistence_worker", None)
        if worker is None:
            return None
        return worker.snapshot()

    def _market_discovery_snapshot(self) -> dict[str, Any]:
        state = getattr(self.runtime, "market_discovery_scan", None)
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
            "round_id": int(getattr(state, "round_id", 0)),
            "cursor_active": getattr(state, "after_cursor", None) is not None
            or bool(getattr(state, "query_cursors", {})),
            "query_cursors": jsonable(getattr(state, "query_cursors", {})),
            "completed_query_names": sorted(str(name) for name in getattr(state, "completed_query_names", ())),
            "active_cursor_count": len(getattr(state, "query_cursors", {})),
            "completed_query_count": len(getattr(state, "completed_query_names", ())),
            "round_started_at": jsonable(getattr(state, "round_started_at", None)),
            "last_round_completed_at": jsonable(getattr(state, "last_round_completed_at", None)),
            "pages_scanned_in_round": int(getattr(state, "pages_scanned_in_round", 0)),
            "markets_seen_in_round": int(getattr(state, "markets_seen_in_round", 0)),
            "last_completed_round_pages": int(getattr(state, "last_completed_round_pages", 0)),
            "last_completed_round_markets": int(getattr(state, "last_completed_round_markets", 0)),
            "last_page_size": int(getattr(state, "last_page_size", 0)),
            "last_tick_started_at": jsonable(getattr(state, "last_tick_started_at", None)),
            "last_tick_completed_at": jsonable(getattr(state, "last_tick_completed_at", None)),
            "last_tick_requests": int(getattr(state, "last_tick_requests", 0)),
            "last_tick_markets": int(getattr(state, "last_tick_markets", 0)),
            "last_error": getattr(state, "last_error", None),
            "consecutive_failures": int(getattr(state, "consecutive_failures", 0)),
        }

    def _market_ws_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        worker = getattr(self.runtime, "market_ws_worker", None)
        if worker is None:
            return None
        snapshot = getattr(worker, "snapshot", None)
        if not callable(snapshot):
            return None
        return snapshot(token_id)

    def _settings(self) -> Any | None:
        return getattr(self.runtime, "settings", None)
