from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api import ExtensionContext
from polymarket_trader.extension_api.decisions import (
    EntrySizing,
    ExtensionDecision,
    RecoveryDecision,
    UniverseDecision,
)
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry, MarketRegistrySnapshot
from polymarket_trader.app.reconcile_service import ReconcileService
from polymarket_trader.main import _is_reconcile_trigger
from polymarket_trader.workers.market_ws_worker import MarketWsWorker
from polymarket_trader.workers.reconcile_authority_refresher import ReconcileAuthorityRefresher
from polymarket_trader.workers.reconcile_worker import ReconcileWorker


class _NoopHooks:
    def discovery_queries(self):
        return ()

    def select_market(self, market: Market) -> UniverseDecision:
        return UniverseDecision.include(reason="test")

    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        raise NotImplementedError

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="test")

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="test")

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        return RecoveryDecision(reason="test")

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return ()

    def should_keep_tracking(self, market: Market, account_snapshot: AccountSnapshot | None) -> bool:
        return False

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return existing_market


class _CountingGammaClient:
    def __init__(self) -> None:
        self.slugs: list[str | None] = []

    async def list_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        tag: str | None = None,
        slug: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout_s: float | None = None,
    ) -> tuple[object, ...]:
        self.slugs.append(slug)
        return ()


class _TerminalLiveStateHooks(_NoopHooks):
    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        return RecoveryDecision(
            reason="test",
            pause_trading=True,
            pause_reason="sports_live_state_ended",
        )


def _market(index: int) -> Market:
    return Market(
        condition_id=f"condition-{index}",
        market_slug=f"market-{index}",
        outcomes=(
            MarketOutcome(token_id=f"token-{index}-yes", outcome="YES"),
            MarketOutcome(token_id=f"token-{index}-no", outcome="NO"),
        ),
    )


def test_market_discovery_events_do_not_drive_reconcile_queue() -> None:
    event = DomainEvent(
        trace_id="trace-1",
        event_type=DomainEventType.MARKET_DISCOVERED,
        event_id="event-1",
        condition_id="condition-1",
        reason="market_discovered",
    )

    assert _is_reconcile_trigger(event) is False


@pytest.mark.asyncio
async def test_discovery_reconcile_event_does_not_refresh_market_authority() -> None:
    market = _market(1)
    registry = MarketRegistry()
    registry.upsert(market)
    event_bus = EventBus()
    gamma = _CountingGammaClient()
    worker = ReconcileWorker(
        event_bus=event_bus,
        reconcile_service=ReconcileService(extension_hooks=_NoopHooks()),
        registry_snapshot_provider=registry.snapshot,
        gamma_client=gamma,
    )

    await event_bus.publish(
        OutboxPriority.P2,
        DomainEvent(
            trace_id="trace-1",
            event_type=DomainEventType.MARKET_DISCOVERED,
            event_id="event-1",
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            reason="market_discovered",
        ),
    )

    await worker.run_once()

    assert gamma.slugs == []


@pytest.mark.asyncio
async def test_refresher_skips_market_authority_when_disabled() -> None:
    market = _market(1)
    gamma = _CountingGammaClient()
    refresher = ReconcileAuthorityRefresher(
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        gamma_client=gamma,
    )

    summary = await refresher.refresh(trace_id="trace-1", refresh_market_authority=False)

    assert summary.market_count == 1
    assert summary.refreshed_markets == 0
    assert gamma.slugs == []


@pytest.mark.asyncio
async def test_unscoped_refresher_only_refreshes_markets_with_exposure() -> None:
    idle_market = _market(1)
    exposed_market = _market(2)
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            condition_id=exposed_market.condition_id,
            token_id=exposed_market.token_ids[0],
            shares=Decimal("1"),
            cost_usdc=Decimal("0.5"),
        )
    )
    gamma = _CountingGammaClient()
    refresher = ReconcileAuthorityRefresher(
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((idle_market, exposed_market)),
        account_state_store=account_state,
        gamma_client=gamma,
    )

    await refresher.refresh(trace_id="trace-1")

    assert gamma.slugs == [exposed_market.market_slug]


@pytest.mark.asyncio
async def test_reconcile_prunes_expired_idle_market_from_registry_and_market_ws() -> None:
    expired_market = _market(1).with_metadata(
        end_date=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    active_market = _market(2).with_metadata(
        end_date=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    registry = MarketRegistry()
    registry.upsert(expired_market)
    registry.upsert(active_market)
    market_ws_worker = MarketWsWorker(registry=registry)
    market_ws_worker.track_market(expired_market)
    market_ws_worker.track_market(active_market)
    account_state = AccountStateStore()
    worker = ReconcileWorker(
        reconcile_service=ReconcileService(extension_hooks=_NoopHooks()),
        registry_snapshot_provider=registry.snapshot,
        account_state_store=account_state,
        registry=registry,
        market_ws_worker=market_ws_worker,
    )

    await worker.reconcile_once(trace_id="trace-1", refresh_market_authority=False)

    assert registry.get_by_condition_id(expired_market.condition_id) is None
    assert registry.get_by_condition_id(active_market.condition_id) == active_market
    status = market_ws_worker.status_snapshot()
    assert not set(expired_market.token_ids) & set(status.tracked_token_ids)
    assert set(active_market.token_ids) <= set(status.tracked_token_ids)


@pytest.mark.asyncio
async def test_reconcile_keeps_expired_market_with_position_subscribed() -> None:
    expired_market = _market(1).with_metadata(
        end_date=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    registry = MarketRegistry()
    registry.upsert(expired_market)
    market_ws_worker = MarketWsWorker(registry=registry)
    market_ws_worker.track_market(expired_market)
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            condition_id=expired_market.condition_id,
            token_id=expired_market.token_ids[0],
            shares=Decimal("1"),
            cost_usdc=Decimal("0.5"),
        )
    )
    worker = ReconcileWorker(
        reconcile_service=ReconcileService(extension_hooks=_NoopHooks()),
        registry_snapshot_provider=registry.snapshot,
        account_state_store=account_state,
        registry=registry,
        market_ws_worker=market_ws_worker,
    )

    await worker.reconcile_once(trace_id="trace-1", refresh_market_authority=False)

    assert registry.get_by_condition_id(expired_market.condition_id) == expired_market
    status = market_ws_worker.status_snapshot()
    assert set(expired_market.token_ids) <= set(status.tracked_token_ids)


@pytest.mark.asyncio
async def test_reconcile_prunes_idle_market_after_terminal_live_state_pause() -> None:
    market = _market(1).with_metadata(
        end_date=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    registry = MarketRegistry()
    registry.upsert(market)
    market_ws_worker = MarketWsWorker(registry=registry)
    market_ws_worker.track_market(market)
    account_state = AccountStateStore()
    worker = ReconcileWorker(
        reconcile_service=ReconcileService(extension_hooks=_TerminalLiveStateHooks()),
        registry_snapshot_provider=registry.snapshot,
        account_state_store=account_state,
        registry=registry,
        market_ws_worker=market_ws_worker,
    )

    await worker.reconcile_once(trace_id="trace-1", refresh_market_authority=False)

    assert registry.get_by_condition_id(market.condition_id) is None
    status = market_ws_worker.status_snapshot()
    assert not set(market.token_ids) & set(status.tracked_token_ids)
