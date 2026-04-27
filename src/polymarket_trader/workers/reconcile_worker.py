from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from polymarket_trader.app.reconcile_service import (
    ReconcileAction,
    ReconcilePlan,
    ReconcileService,
)
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.account import AccountSnapshot, MarketPauseSource
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry, MarketRegistrySnapshot
from polymarket_trader.serialization import jsonable
from polymarket_trader.workers.market_ws_worker import MarketWsWorker
from polymarket_trader.workers.reconcile_action_applier import ReconcileActionApplier
from polymarket_trader.workers.reconcile_authority_refresher import (
    AuthoritativeRefreshFailure,
    AuthoritativeRefreshSummary,
    DataAuthorityClient,
    MarketAuthorityClient,
    OrderAuthorityClient,
    ReconcileAuthorityRefresher,
    TradingAuthorityClient,
)

RegistrySnapshotProvider = Callable[[], MarketRegistrySnapshot]
AccountSnapshotProvider = Callable[[], AccountSnapshot]


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ReconcileWorkerResult:
    trace_id: str
    plan: ReconcilePlan
    applied_actions: tuple[ReconcileAction, ...]
    failed_actions: tuple[tuple[ReconcileAction, str], ...]


@dataclass(frozen=True, slots=True)
class ReconcileWorkerResultSummary:
    trace_id: str
    trigger_event_type: str | None
    started_at: datetime
    completed_at: datetime
    market_count: int
    diff_count: int
    action_count: int
    applied_action_count: int
    failed_action_count: int
    refresh_failures: tuple[AuthoritativeRefreshFailure, ...]


@dataclass(frozen=True, slots=True)
class ReconcileWorkerStatus:
    running: bool
    last_trace_id: str | None
    last_trigger_event_type: str | None
    last_started_at: datetime | None
    last_completed_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    last_refresh_summary: AuthoritativeRefreshSummary | None
    last_result: ReconcileWorkerResultSummary | None
    recent_results: tuple[ReconcileWorkerResultSummary, ...]


class ReconcileWorker:
    priority = "P2"

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        reconcile_service: ReconcileService | None = None,
        registry_snapshot_provider: RegistrySnapshotProvider | None = None,
        account_snapshot_provider: AccountSnapshotProvider | None = None,
        account_state_store: AccountStateStore | None = None,
        trading_service: TradingService | None = None,
        registry: MarketRegistry | None = None,
        market_ws_worker: MarketWsWorker | None = None,
        gamma_client: MarketAuthorityClient | None = None,
        clob_client: OrderAuthorityClient | None = None,
        data_client: DataAuthorityClient | None = None,
        trading_client: TradingAuthorityClient | None = None,
    ) -> None:
        self._event_bus = event_bus
        if reconcile_service is None:
            raise ValueError("reconcile_service is required")
        self._reconcile_service = reconcile_service
        self._registry_snapshot_provider = registry_snapshot_provider
        self._account_snapshot_provider = account_snapshot_provider
        self._account_state_store = account_state_store
        self._trading_service = trading_service
        self._registry = registry
        self._market_ws_worker = market_ws_worker
        self._authority_refresher = ReconcileAuthorityRefresher(
            registry_snapshot_provider=registry_snapshot_provider,
            account_state_store=account_state_store,
            registry=registry,
            market_ws_worker=market_ws_worker,
            gamma_client=gamma_client,
            clob_client=clob_client,
            data_client=data_client,
            trading_client=trading_client,
        )
        self._running = False
        self._last_trace_id: str | None = None
        self._last_trigger_event_type: str | None = None
        self._last_started_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._last_refresh_summary: AuthoritativeRefreshSummary | None = None
        self._last_result: ReconcileWorkerResultSummary | None = None
        self._recent_results: deque[ReconcileWorkerResultSummary] = deque(maxlen=8)
        self._action_applier = ReconcileActionApplier(
            trading_service=trading_service,
            account_state_store=account_state_store,
        )

    async def run(self) -> None:
        if self._event_bus is None:
            raise RuntimeError("ReconcileWorker requires an EventBus to run")
        while True:
            await self.run_once()

    async def run_once(self) -> ReconcileWorkerResult:
        trigger = None
        if self._event_bus is not None:
            trigger = await self._event_bus.next_maintenance_event()
        trace_id = getattr(trigger, "trace_id", None) or uuid4().hex
        started_at = _utc_now()
        self._running = True
        self._last_started_at = started_at
        self._last_trace_id = trace_id
        self._last_trigger_event_type = None if trigger is None else str(trigger.event_type)
        try:
            result = await self.reconcile_once(trace_id=trace_id, trigger_event=trigger)
        except Exception as exc:
            self._last_error = str(exc)
            self._last_completed_at = _utc_now()
            raise
        else:
            completed_at = _utc_now()
            self._last_completed_at = completed_at
            self._last_success_at = completed_at
            self._last_error = None
            summary = ReconcileWorkerResultSummary(
                trace_id=result.trace_id,
                trigger_event_type=self._last_trigger_event_type,
                started_at=started_at,
                completed_at=completed_at,
                market_count=len(result.plan.market_plans),
                diff_count=result.plan.diff_count,
                action_count=sum(len(plan.actions) for plan in result.plan.market_plans),
                applied_action_count=len(result.applied_actions),
                failed_action_count=len(result.failed_actions),
                refresh_failures=self._last_refresh_summary.failures
                if self._last_refresh_summary is not None
                else (),
            )
            self._last_result = summary
            self._recent_results.append(summary)
            return result
        finally:
            self._running = False

    def status_snapshot(self) -> ReconcileWorkerStatus:
        return ReconcileWorkerStatus(
            running=self._running,
            last_trace_id=self._last_trace_id,
            last_trigger_event_type=self._last_trigger_event_type,
            last_started_at=self._last_started_at,
            last_completed_at=self._last_completed_at,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            last_refresh_summary=self._last_refresh_summary,
            last_result=self._last_result,
            recent_results=tuple(self._recent_results),
        )

    async def reconcile_once(
        self,
        *,
        trace_id: str | None = None,
        trigger_event: DomainEvent | None = None,
        condition_ids: tuple[str, ...] | None = None,
    ) -> ReconcileWorkerResult:
        trace_id = trace_id or getattr(trigger_event, "trace_id", None) or uuid4().hex
        self._running = True
        self._last_started_at = _utc_now()
        self._last_trace_id = trace_id
        self._last_trigger_event_type = None if trigger_event is None else str(trigger_event.event_type)
        refresh_summary = await self._refresh_authoritative_state(
            trace_id=trace_id,
            condition_ids=condition_ids,
        )
        self._last_refresh_summary = refresh_summary
        if refresh_summary.failures:
            logger.warning(
                "reconcile authoritative refresh degraded",
                extra={
                    "trace_id": trace_id,
                    "failure_count": len(refresh_summary.failures),
                    "failures": refresh_summary.failures,
                },
            )
        registry_snapshot, account_snapshot = self._resolve_snapshots()
        plan = self._reconcile_service.build_reconcile_plan(
            registry_snapshot=registry_snapshot,
            account_snapshot=account_snapshot,
            trace_id=trace_id,
            condition_ids=condition_ids,
        )
        await self._publish(
            OutboxPriority.P3,
            DomainEvent(
                trace_id=trace_id,
                event_type=DomainEventType.RECONCILE_STARTED,
                event_id=uuid4().hex,
                reason="reconcile_started",
                created_at=_utc_now(),
                payload={
                    "market_count": len(plan.market_plans),
                    "paused_market_count": plan.paused_market_count,
                    "diff_count": plan.diff_count,
                    "trigger_event_type": None if trigger_event is None else str(trigger_event.event_type),
                    "refresh_summary": refresh_summary.as_payload(),
                },
            ),
        )

        applied_actions: list[ReconcileAction] = []
        failed_actions: list[tuple[ReconcileAction, str]] = []
        working_snapshot = account_snapshot

        for market_plan in plan.market_plans:
            if market_plan.pause_trading:
                await self._publish(
                    OutboxPriority.P1,
                    DomainEvent(
                        trace_id=trace_id,
                        event_type=DomainEventType.TRADING_PAUSED_FOR_MARKET,
                        event_id=uuid4().hex,
                        market_slug=market_plan.market.market_slug,
                        condition_id=market_plan.market.condition_id,
                        token_id=None,
                        reason=market_plan.pause_reason or "market_not_tradable",
                        created_at=_utc_now(),
                        payload={
                            "market_status": market_plan.market.trading_status.value,
                            "pause_reason": market_plan.pause_reason,
                        },
                    ),
                )
                if self._account_state_store is not None:
                    self._account_state_store.pause_market(
                        market_plan.market.condition_id,
                        reason=market_plan.pause_reason or "market_not_tradable",
                        source=MarketPauseSource.RECONCILE,
                    )
                    working_snapshot = self._account_state_store.snapshot()

            for action in market_plan.actions:
                await self._publish_diff(trace_id, market_plan.market, action)
                try:
                    await self._action_applier.apply(action, market_plan.market, working_snapshot)
                    applied_actions.append(action)
                    if self._account_state_store is not None:
                        working_snapshot = self._account_state_store.snapshot()
                except Exception as exc:  # pragma: no cover - injected adapters can fail
                    failed_actions.append((action, str(exc)))

            if market_plan.has_changes:
                await self._publish(
                    OutboxPriority.P3,
                    DomainEvent(
                        trace_id=trace_id,
                        event_type=DomainEventType.RECONCILE_APPLIED,
                        event_id=uuid4().hex,
                        market_slug=market_plan.market.market_slug,
                        condition_id=market_plan.market.condition_id,
                        token_id=None,
                        reason="reconcile_applied",
                        created_at=_utc_now(),
                        payload={
                            "action_count": len(market_plan.actions),
                            "applied_count": sum(
                                1 for item in applied_actions if item.condition_id == market_plan.market.condition_id
                            ),
                            "failed_count": sum(
                                1 for item, _ in failed_actions if item.condition_id == market_plan.market.condition_id
                            ),
                        },
                    ),
            )

        if self._account_state_store is not None:
            self._account_state_store.mark_reconciled()
            self._prune_out_of_universe_markets(self._account_state_store.snapshot())

        completed_at = _utc_now()
        self._last_completed_at = completed_at
        self._last_success_at = completed_at
        self._last_error = None

        return ReconcileWorkerResult(
            trace_id=trace_id,
            plan=plan,
            applied_actions=tuple(applied_actions),
            failed_actions=tuple(failed_actions),
        )

    async def _publish_diff(self, trace_id: str, market: Market, action: ReconcileAction) -> None:
        await self._publish(
            OutboxPriority.P3,
            DomainEvent(
                trace_id=trace_id,
                event_type=DomainEventType.RECONCILE_DIFF_DETECTED,
                event_id=uuid4().hex,
                market_slug=market.market_slug,
                condition_id=market.condition_id,
                token_id=action.token_id,
                reason=action.reason,
                created_at=_utc_now(),
                payload={
                    "action_type": action.action_type.value,
                    "source_order_id": action.source_order_id,
                    "source_order_side": None if action.source_order_side is None else action.source_order_side.value,
                    "target_size_shares": None if action.target_size_shares is None else str(action.target_size_shares),
                    "target_notional_usdc": None if action.target_notional_usdc is None else str(action.target_notional_usdc),
                    "pause_reason": action.pause_reason,
                    "metadata": jsonable(action.metadata),
                },
            ),
        )

    async def _publish(self, priority: OutboxPriority, event: DomainEvent) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(priority, event)

    def _prune_out_of_universe_markets(self, account_snapshot: AccountSnapshot) -> None:
        if self._registry is None:
            return
        markets = self._registry.snapshot().markets
        for market in markets:
            if market.trading_status != TradingStatus.PAUSED:
                continue
            if market.reject_reason != "market_out_of_universe":
                continue
            if _market_has_exposure(account_snapshot, market):
                continue
            self._registry.remove_market(market.condition_id)
            if self._market_ws_worker is not None and hasattr(self._market_ws_worker, "untrack_market"):
                self._market_ws_worker.untrack_market(market.token_ids)

    def _resolve_snapshots(self) -> tuple[MarketRegistrySnapshot, AccountSnapshot]:
        if self._registry is not None:
            registry_snapshot = self._registry.snapshot()
        elif self._registry_snapshot_provider is not None:
            registry_snapshot = self._registry_snapshot_provider()
        else:
            registry_snapshot = MarketRegistrySnapshot(tuple())

        if self._account_state_store is not None:
            account_snapshot = self._account_state_store.snapshot()
        elif self._account_snapshot_provider is not None:
            account_snapshot = self._account_snapshot_provider()
        else:
            account_snapshot = AccountSnapshot()

        return registry_snapshot, account_snapshot

    async def _refresh_authoritative_state(
        self,
        *,
        trace_id: str,
        condition_ids: tuple[str, ...] | None = None,
    ) -> AuthoritativeRefreshSummary:
        return await self._authority_refresher.refresh(
            trace_id=trace_id,
            condition_ids=condition_ids,
        )


def _market_has_exposure(account_snapshot: AccountSnapshot, market: Market) -> bool:
    for token_id in market.token_ids:
        position = account_snapshot.get_position(market.condition_id, token_id)
        if position is not None and (
            position.shares > 0
            or position.open_buy_shares > 0
            or position.open_sell_shares > 0
            or position.pending_buy_shares > 0
        ):
            return True
        if account_snapshot.open_orders_for_market(market.condition_id, token_id):
            return True
    return False
