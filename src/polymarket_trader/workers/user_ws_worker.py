from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterable, Mapping
from uuid import uuid4

from polymarket_trader.domain.events import DomainEvent, OutboxPriority
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.workers.user_ws_projection import (
    UserWsAccountProjector,
    extract_trace_id as _extract_trace_id,
    flatten_message as _flatten_message,
    message_type as _message_type,
)

MessageSource = Callable[[], Awaitable[Mapping[str, Any] | DomainEvent | None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class UserWsProcessResult:
    message_type: str
    events: tuple[DomainEvent, ...]
    snapshot: AccountSnapshot
    connected: bool

    @property
    def emitted(self) -> int:
        return len(self.events)


@dataclass(frozen=True, slots=True)
class UserWsResultSummary:
    trace_id: str
    message_type: str
    event_count: int
    connected: bool
    allow_new_entries: bool
    condition_id: str | None
    token_id: str | None
    market_slug: str | None
    reason: str
    balance_usdc: Decimal
    allowance_usdc: Decimal
    open_order_count: int
    position_count: int
    fill_count: int
    last_reconcile_at: datetime | None
    created_at: datetime = field(default_factory=_utc_now)


@dataclass(frozen=True, slots=True)
class UserWsSubscriptionStatus:
    condition_id: str
    subscribed_at: datetime
    last_message_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class UserWsWorkerStatus:
    connected: bool
    allow_new_entries: bool
    subscribed_condition_ids: tuple[str, ...]
    subscription_count: int
    last_message_at: datetime | None
    last_connected_at: datetime | None
    last_disconnected_at: datetime | None
    last_reconcile_at: datetime | None
    last_error: str | None
    last_result: UserWsResultSummary | None
    recent_results: tuple[UserWsResultSummary, ...]
    balance_usdc: Decimal
    allowance_usdc: Decimal
    open_order_count: int
    position_count: int
    fill_count: int
    paused_market_count: int
    subscriptions: tuple[UserWsSubscriptionStatus, ...]


class UserWsWorker:
    priority = "P0"

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        account_state_store: AccountStateStore | None = None,
        message_source: MessageSource | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._account_state = account_state_store or AccountStateStore()
        self._account_projector = UserWsAccountProjector(self._account_state)
        self._message_source = message_source
        self._subscribed_condition_ids: dict[str, datetime] = {}
        self._last_message_at: datetime | None = None
        self._last_connected_at: datetime | None = None
        self._last_disconnected_at: datetime | None = None
        self._last_error: str | None = None
        self._last_result: UserWsResultSummary | None = None
        self._recent_results: deque[UserWsResultSummary] = deque(maxlen=8)

    def snapshot(self) -> AccountSnapshot:
        return self._account_state.snapshot()

    def status_snapshot(self, *, include_subscriptions: bool = True) -> UserWsWorkerStatus:
        account_snapshot = self._account_state.snapshot()
        if not include_subscriptions:
            return UserWsWorkerStatus(
                connected=account_snapshot.user_ws_connected,
                allow_new_entries=account_snapshot.allow_new_entries,
                subscribed_condition_ids=(),
                subscription_count=len(self._subscribed_condition_ids),
                last_message_at=self._last_message_at,
                last_connected_at=self._last_connected_at,
                last_disconnected_at=self._last_disconnected_at,
                last_reconcile_at=account_snapshot.last_reconcile_at,
                last_error=self._last_error,
                last_result=self._last_result,
                recent_results=tuple(self._recent_results),
                balance_usdc=account_snapshot.balance_usdc,
                allowance_usdc=account_snapshot.allowance_usdc,
                open_order_count=len(account_snapshot.open_orders),
                position_count=len(account_snapshot.positions),
                fill_count=len(account_snapshot.fills),
                paused_market_count=len(account_snapshot.market_pauses),
                subscriptions=(),
            )
        subscribed_condition_ids = tuple(sorted(self._subscribed_condition_ids))
        subscriptions = tuple(
            UserWsSubscriptionStatus(
                condition_id=condition_id,
                subscribed_at=self._subscribed_condition_ids[condition_id],
                last_message_at=self._last_message_at,
                last_error=self._last_error,
            )
            for condition_id in subscribed_condition_ids
        )
        return UserWsWorkerStatus(
            connected=account_snapshot.user_ws_connected,
            allow_new_entries=account_snapshot.allow_new_entries,
            subscribed_condition_ids=subscribed_condition_ids,
            subscription_count=len(subscribed_condition_ids),
            last_message_at=self._last_message_at,
            last_connected_at=self._last_connected_at,
            last_disconnected_at=self._last_disconnected_at,
            last_reconcile_at=account_snapshot.last_reconcile_at,
            last_error=self._last_error,
            last_result=self._last_result,
            recent_results=tuple(self._recent_results),
            balance_usdc=account_snapshot.balance_usdc,
            allowance_usdc=account_snapshot.allowance_usdc,
            open_order_count=len(account_snapshot.open_orders),
            position_count=len(account_snapshot.positions),
            fill_count=len(account_snapshot.fills),
            paused_market_count=len(account_snapshot.market_pauses),
            subscriptions=subscriptions,
        )

    def build_subscription_request(
        self,
        condition_ids: str | Iterable[str],
        *,
        auth: Mapping[str, str],
    ) -> dict[str, Any]:
        # User Channel 按官方当前 markets=condition_ids 订阅。
        normalized = (
            (condition_ids.strip(),)
            if isinstance(condition_ids, str) and condition_ids.strip()
            else tuple(
                str(condition_id).strip()
                for condition_id in condition_ids
                if str(condition_id).strip()
            )
        )
        subscribed_at = _utc_now()
        for condition_id in normalized:
            self._subscribed_condition_ids[condition_id] = subscribed_at
        return {
            "auth": {str(key): str(value) for key, value in auth.items()},
            "markets": list(normalized),
            "type": "user",
        }

    def record_error(self, reason: str) -> None:
        self._last_error = reason

    async def run(self) -> None:
        if self._message_source is None:
            raise RuntimeError("UserWsWorker requires a message_source to run")
        while True:
            message = await self._message_source()
            if message is None:
                continue
            await self.process_message(message)

    async def run_once(self) -> UserWsProcessResult | None:
        if self._message_source is None:
            raise RuntimeError("UserWsWorker requires a message_source to run")
        message = await self._message_source()
        if message is None:
            return None
        return await self.process_message(message)

    async def process_message(
        self,
        message: Mapping[str, Any] | DomainEvent,
    ) -> UserWsProcessResult:
        self._last_message_at = _utc_now()
        self._last_error = None
        payload = _flatten_message(message)
        message_type = _message_type(payload)
        trace_id = _extract_trace_id(payload)

        if message_type in {"connected", "reconnected", "connection_open", "ws_connected"}:
            snapshot = self._account_state.mark_user_ws_connected(True)
            self._record_connection_state(True)
            return await self._emit_result(
                trace_id=trace_id,
                message_type=message_type,
                snapshot=snapshot,
                events=(),
                reason="connected",
                connected=True,
            )

        if message_type in {"disconnected", "disconnect", "connection_closed", "ws_disconnected"}:
            snapshot = self._account_state.mark_user_ws_connected(False)
            self._record_connection_state(False)
            return await self._emit_result(
                trace_id=trace_id,
                message_type=message_type,
                snapshot=snapshot,
                events=(),
                reason="disconnected",
                connected=False,
            )

        events: list[DomainEvent] = []

        if self._has_balance_fields(payload) or message_type == "balance":
            events.extend(
                await self._account_projector.handle_balance(payload, trace_id=trace_id)
            )

        if self._has_position_fields(payload) or message_type in {"position", "positions"}:
            events.extend(
                await self._account_projector.handle_position(payload, trace_id=trace_id)
            )

        if self._has_order_fields(payload) or message_type in {"order", "orders"}:
            events.extend(
                await self._account_projector.handle_order(payload, trace_id=trace_id)
            )

        if self._has_fill_fields(payload) or message_type in {"trade", "fill", "fills"}:
            events.extend(
                await self._account_projector.handle_fill(payload, trace_id=trace_id)
            )

        snapshot = self._account_state.snapshot()
        return await self._emit_result(
            trace_id=trace_id,
            message_type=message_type or "snapshot",
            snapshot=snapshot,
            events=tuple(events),
            reason="message_processed",
        )

    async def set_connection_state(
        self,
        connected: bool,
        *,
        trace_id: str | None = None,
        reason: str | None = None,
    ) -> UserWsProcessResult:
        trace_id = trace_id or uuid4().hex
        self._last_message_at = _utc_now()
        if connected:
            self._last_error = None
        if connected:
            snapshot = self._account_state.mark_user_ws_connected(True)
        else:
            snapshot = self._account_state.mark_user_ws_connected(False)
        self._record_connection_state(connected)
        return await self._emit_result(
            trace_id=trace_id,
            message_type="connection_state",
            snapshot=snapshot,
            events=(),
            reason=reason or "connection_state",
            connected=connected,
        )

    async def _emit_result(
        self,
        *,
        trace_id: str,
        message_type: str,
        snapshot: AccountSnapshot,
        events: tuple[DomainEvent, ...],
        reason: str = "",
        connected: bool | None = None,
    ) -> UserWsProcessResult:
        if self._event_bus is not None:
            for event in events:
                await self._event_bus.publish(OutboxPriority.P0, event)
        result = UserWsProcessResult(
            message_type=message_type,
            events=events,
            snapshot=snapshot,
            connected=snapshot.user_ws_connected if connected is None else connected,
        )
        summary = UserWsResultSummary(
            trace_id=trace_id,
            message_type=message_type,
            event_count=len(events),
            connected=result.connected,
            allow_new_entries=snapshot.allow_new_entries,
            condition_id=events[0].condition_id if events else None,
            token_id=events[0].token_id if events else None,
            market_slug=events[0].market_slug if events else None,
            reason=reason,
            balance_usdc=snapshot.balance_usdc,
            allowance_usdc=snapshot.allowance_usdc,
            open_order_count=len(snapshot.open_orders),
            position_count=len(snapshot.positions),
            fill_count=len(snapshot.fills),
            last_reconcile_at=snapshot.last_reconcile_at,
        )
        self._last_result = summary
        self._recent_results.append(summary)
        return result

    def _record_connection_state(self, connected: bool) -> None:
        now = _utc_now()
        if connected:
            self._last_connected_at = now
        else:
            self._last_disconnected_at = now

    def _has_balance_fields(self, payload: Mapping[str, Any]) -> bool:
        return any(
            key in payload
            for key in (
                "balance_usdc",
                "available_balance_usdc",
                "usdc_balance",
                "balance",
                "allowance_usdc",
                "allowance",
            )
        )

    def _has_position_fields(self, payload: Mapping[str, Any]) -> bool:
        return any(key in payload for key in ("position", "positions", "holdings"))

    def _has_order_fields(self, payload: Mapping[str, Any]) -> bool:
        return any(key in payload for key in ("order", "orders", "open_orders"))

    def _has_fill_fields(self, payload: Mapping[str, Any]) -> bool:
        return any(key in payload for key in ("trade", "trades", "fill", "fills"))
