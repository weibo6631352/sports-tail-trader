from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.order import Order, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
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
from polymarket_trader.app.reconcile_service import ReconcileActionType
from polymarket_trader.main import _is_reconcile_trigger
from polymarket_trader.main import _runtime_trace_id
from polymarket_trader.workers.market_ws import MarketWsWorker
from polymarket_trader.workers.reconcile import ReconcileAuthorityRefresher
from polymarket_trader.workers.reconcile import ReconcileWorker


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


class _CaptureRecoveryHooks(_NoopHooks):
    def __init__(self) -> None:
        self.recovery_context: ExtensionContext | None = None

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        self.recovery_context = context
        return RecoveryDecision(reason="captured")


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


class _GammaMarketCandidate:
    def __init__(self, market: Market) -> None:
        self.condition_id = market.condition_id
        self.market_slug = market.market_slug
        self.clob_enabled = True
        self.outcomes = market.outcomes
        self._market = market

    def to_market(self) -> Market:
        return self._market


class _MarketBySlugGammaClient:
    def __init__(self, market: Market) -> None:
        self.market = market
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
    ) -> tuple[_GammaMarketCandidate, ...]:
        self.slugs.append(slug)
        if slug == self.market.market_slug:
            return (_GammaMarketCandidate(self.market),)
        return ()


class _PositionDTO:
    def __init__(self, position: Position) -> None:
        self._position = position

    def to_position(self, *, strategy_id: str) -> Position:
        # 测试 mock 保持与生产 DTO 同签名：strategy_id 由调用方传入。
        return self._position


class _PositionDataClient:
    has_auth_client = True

    def __init__(self, positions: tuple[Position, ...]) -> None:
        self.positions = positions

    async def list_positions(self) -> tuple[_PositionDTO, ...]:
        return tuple(_PositionDTO(position) for position in self.positions)


class _OrderDTO:
    def __init__(self, order: Order) -> None:
        self._order = order

    def to_order_record(self, *, strategy_id: str) -> Order:
        return self._order


class _OpenOrdersClient:
    has_auth_client = True

    def __init__(self, orders: tuple[Order, ...]) -> None:
        self.orders = orders

    async def list_open_orders(self) -> tuple[_OrderDTO, ...]:
        return tuple(_OrderDTO(order) for order in self.orders)

    async def list_fills(self) -> tuple[object, ...]:
        return ()

    async def get_balance_allowance(self) -> None:
        return None


class _TerminalLiveStateHooks(_NoopHooks):
    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        return RecoveryDecision(
            reason="test",
            pause_trading=True,
            pause_reason="sports_live_state_ended",
        )


class _TerminalLiveStateExitHooks(_TerminalLiveStateHooks):
    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        assert context.position is not None
        return ExtensionDecision.sell(
            reason="strategy_exit",
            token_id=context.position.token_id,
            price=Decimal("0.99"),
            size_shares=context.position.shares - context.position.open_sell_shares,
            market_slug=context.market.market_slug if context.market is not None else None,
            metadata={"exit_trigger": context.metadata.get("exit_trigger")},
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


def test_runtime_trace_id_fits_persistence_columns() -> None:
    trace_id = _runtime_trace_id("reconcile", source="event:reconcile_scheduled")

    assert len(trace_id) <= 64
    assert trace_id.startswith("reconcile-")


def test_reconcile_recovery_context_includes_orderbook_snapshot() -> None:
    market = _market(1)
    orderbook = OrderbookSnapshot(
        token_id="token-1-yes",
        condition_id="condition-1",
        best_bid=Decimal("0.99"),
        best_ask=Decimal("1"),
        bids=(PriceLevel(price=Decimal("0.99"), size=Decimal("10")),),
        asks=(PriceLevel(price=Decimal("1"), size=Decimal("10")),),
        received_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        tick_size=Decimal("0.01"),
    )
    hooks = _CaptureRecoveryHooks()
    service = ReconcileService(
        strategy_id="sports_tail",
        extension_hooks=hooks,
        orderbook_reader=lambda token_id: orderbook if token_id == "token-1-yes" else None,
    )

    service.build_reconcile_plan(
        registry_snapshot=MarketRegistrySnapshot((market,)),
        account_snapshot=AccountSnapshot(
            positions=(
                Position(
                    strategy_id="sports_tail",
                    condition_id="condition-1",
                    token_id="token-1-yes",
                    shares=Decimal("5"),
                    cost_usdc=Decimal("4.95"),
                ),
            ),
            allow_new_entries=True,
        ),
    )

    assert hooks.recovery_context is not None
    token_views = {view.token_id: view for view in hooks.recovery_context.market_token_views}
    assert token_views["token-1-yes"].orderbook == orderbook
    assert token_views["token-1-no"].orderbook is None


@pytest.mark.asyncio
async def test_discovery_reconcile_event_does_not_refresh_market_authority() -> None:
    market = _market(1)
    registry = MarketRegistry()
    registry.upsert(market)
    event_bus = EventBus()
    gamma = _CountingGammaClient()
    worker = ReconcileWorker(
        event_bus=event_bus,
        reconcile_service=ReconcileService(strategy_id="sports_tail", extension_hooks=_NoopHooks()),
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
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        gamma_client=gamma,
    )

    summary = await refresher.refresh(trace_id="trace-1", refresh_market_authority=False)

    assert summary.market_count == 1
    assert summary.refreshed_markets == 0
    assert gamma.slugs == []


@pytest.mark.asyncio
async def test_refresher_recovers_missing_registry_market_from_account_position_slug() -> None:
    base_market = _market(3)
    market = base_market.with_metadata(event_slug=base_market.market_slug)
    account_state = AccountStateStore()
    registry = MarketRegistry()
    gamma = _MarketBySlugGammaClient(market)
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot(()),
        account_state_store=account_state,
        registry=registry,
        gamma_client=gamma,
        data_client=_PositionDataClient(
            (
                Position(
                    strategy_id="sports_tail",
                    condition_id=market.condition_id,
                    token_id=market.token_ids[0],
                    shares=Decimal("1"),
                    cost_usdc=Decimal("0.5"),
                    market_slug=market.market_slug,
                ),
            )
        ),
    )

    summary = await refresher.refresh(trace_id="trace-1")

    assert summary.refreshed_positions == 1
    assert summary.refreshed_markets == 1
    assert gamma.slugs == [market.market_slug]
    assert registry.get_by_condition_id(market.condition_id) == market


@pytest.mark.asyncio
async def test_refresher_tracks_missing_open_order_market_without_slug_for_recovery() -> None:
    open_buy = Order(
        strategy_id="sports_tail",
        trace_id="trace-buy",
        condition_id="orphan-condition",
        token_id="orphan-token",
        market_slug=None,
        side=OrderSide.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.99"),
        size_shares=Decimal("5.05"),
        remaining_shares=Decimal("5.05"),
        status=OrderStatus.LIVE,
        order_id="orphan-buy",
    )
    account_state = AccountStateStore()
    registry = MarketRegistry()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot(()),
        account_state_store=account_state,
        registry=registry,
        clob_client=_OpenOrdersClient((open_buy,)),
    )

    summary = await refresher.refresh(trace_id="trace-orphan")

    recovered = registry.get_by_condition_id("orphan-condition")
    assert summary.refreshed_open_orders == 1
    assert recovered is not None
    assert recovered.condition_id == "orphan-condition"
    assert recovered.token_ids == ("orphan-token",)


@pytest.mark.asyncio
async def test_refresher_refreshes_position_coverage_from_authoritative_open_orders() -> None:
    market = _market(4)
    position = Position(
        strategy_id="sports_tail",
        condition_id=market.condition_id,
        token_id=market.token_ids[0],
        shares=Decimal("7"),
        cost_usdc=Decimal("5"),
        market_slug=market.market_slug,
    )
    sell_order = Order(
        strategy_id="sports_tail",
        trace_id="trace-sell",
        condition_id=market.condition_id,
        token_id=market.token_ids[0],
        market_slug=market.market_slug,
        side=OrderSide.SELL,
        order_type=OrderType.GTC,
        price=Decimal("0.99"),
        size_shares=Decimal("6"),
        remaining_shares=Decimal("6"),
        status=OrderStatus.LIVE,
        order_id="sell-order",
    )
    account_state = AccountStateStore()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        data_client=_PositionDataClient((position,)),
        clob_client=_OpenOrdersClient((sell_order,)),
    )

    await refresher.refresh(trace_id="trace-coverage")

    refreshed = account_state.snapshot().get_position(market.condition_id, market.token_ids[0])
    assert refreshed is not None
    assert refreshed.open_sell_shares == Decimal("6")


@pytest.mark.asyncio
async def test_unscoped_refresher_only_refreshes_markets_with_exposure() -> None:
    idle_market = _market(1)
    exposed_market = _market(2)
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=exposed_market.condition_id,
            token_id=exposed_market.token_ids[0],
            shares=Decimal("1"),
            cost_usdc=Decimal("0.5"),
        )
    )
    gamma = _CountingGammaClient()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
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
        reconcile_service=ReconcileService(strategy_id="sports_tail", extension_hooks=_NoopHooks()),
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
            strategy_id="sports_tail",
            condition_id=expired_market.condition_id,
            token_id=expired_market.token_ids[0],
            shares=Decimal("1"),
            cost_usdc=Decimal("0.5"),
        )
    )
    worker = ReconcileWorker(
        reconcile_service=ReconcileService(strategy_id="sports_tail", extension_hooks=_NoopHooks()),
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
        reconcile_service=ReconcileService(strategy_id="sports_tail", extension_hooks=_TerminalLiveStateHooks()),
        registry_snapshot_provider=registry.snapshot,
        account_state_store=account_state,
        registry=registry,
        market_ws_worker=market_ws_worker,
    )

    await worker.reconcile_once(trace_id="trace-1", refresh_market_authority=False)

    assert registry.get_by_condition_id(market.condition_id) is None
    status = market_ws_worker.status_snapshot()
    assert not set(market.token_ids) & set(status.tracked_token_ids)


def test_terminal_live_state_pause_keeps_existing_position_exit_in_plan() -> None:
    market = _market(1)
    position = Position(
        strategy_id="sports_tail",
        condition_id=market.condition_id,
        token_id=market.token_ids[0],
        shares=Decimal("7"),
        cost_usdc=Decimal("5"),
    )
    service = ReconcileService(strategy_id="sports_tail", extension_hooks=_TerminalLiveStateExitHooks())

    plan = service.build_market_plan(
        market=market,
        account_snapshot=AccountSnapshot(positions=(position,)),
        trace_id="trace-1",
    )

    assert plan.pause_trading is True
    assert plan.position == position
    action_types = [action.action_type for action in plan.actions]
    assert action_types == [
        ReconcileActionType.SUBMIT_ORDER,
        ReconcileActionType.PAUSE_TRADING,
    ]
    submit_action = plan.actions[0]
    assert submit_action.token_id == market.token_ids[0]
    assert submit_action.source_order_side is not None
    assert submit_action.target_size_shares == Decimal("7")
    assert submit_action.metadata["exit_trigger"] == "reconcile_position"
