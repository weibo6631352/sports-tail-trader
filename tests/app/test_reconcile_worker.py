from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from polymarket_trader.app.reconcile_service import ReconcileActionType, ReconcileService
from polymarket_trader.app.trading_service import TradingReviewResult, TradingService
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    OrderRecord,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.risk import RiskDecision
from polymarket_trader.infra.outbox import LocalOutbox, build_domain_event_outbox_sink
from polymarket_trader.extension_api import (
    EntrySizing,
    ExtensionSpec,
    RecoveryDecision,
    ExtensionContext,
    ExtensionDecision,
    UniverseDecision,
)
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.market_ws_worker import MarketWsWorker
import polymarket_trader.workers.reconcile_authority_refresher as reconcile_authority_refresher_module
from polymarket_trader.workers.reconcile_authority_refresher import ReconcileAuthorityRefresher
from polymarket_trader.workers.reconcile_worker import ReconcileWorker
from strategies.current.strategy import build_strategy
from tests.helpers.markets import build_binary_market


def _no_token_id(market: Market) -> str:
    return market.require_token_id("NO")


class _StubExecutor:
    def __init__(self) -> None:
        self.submitted_intents: list[SellOrderIntent] = []
        self.cancelled_intents: list[CancelOrderIntent] = []
        self.replaced_intents: list[ReplaceOrderIntent] = []

    async def submit(self, intent: SellOrderIntent) -> OrderResult:
        self.submitted_intents.append(intent)
        return OrderResult(
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            status=OrderResultStatus.LIVE,
            intent=intent,
            side=intent.side,
            order_type=intent.order_type,
            price=intent.price,
            requested_size_shares=intent.size_shares,
            notional_usdc=intent.notional_usdc,
            reason="sell_submitted",
        )

    async def cancel(self, intent: CancelOrderIntent) -> OrderResult:
        self.cancelled_intents.append(intent)
        return OrderResult(
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            status=OrderResultStatus.CANCELLED,
            intent=intent,
            reason="cancelled",
        )

    async def replace(self, intent: ReplaceOrderIntent) -> OrderResult:
        self.replaced_intents.append(intent)
        return OrderResult(
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            status=OrderResultStatus.LIVE,
            intent=intent,
            price=intent.new_price,
            requested_size_shares=intent.size_shares,
            remaining_shares=intent.size_shares,
            notional_usdc=intent.new_price * intent.size_shares,
            order_id="sell-2",
            reason="sell_replaced",
        )


class _RejectingTradingService:
    async def review_intent(self, intent: SellOrderIntent, **kwargs: object) -> TradingReviewResult:
        return TradingReviewResult(
            intent=intent,
            operation="sell",
            risk_decision=RiskDecision(
                passed=False,
                reason="risk_rejected",
                trace_id=intent.trace_id,
                suggested_action="reject",
            ),
            submitted=False,
            order_result=OrderResult(
                trace_id=intent.trace_id,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                market_slug=intent.market_slug,
                status=OrderResultStatus.REJECTED,
                intent=intent,
                side=intent.side,
                order_type=intent.order_type,
                price=intent.price,
                requested_size_shares=intent.size_shares,
                notional_usdc=intent.notional_usdc,
                reason="risk_rejected",
            ),
        )


class _ReplaceRecoveryStrategy:
    @property
    def spec(self) -> ExtensionSpec:
        return ExtensionSpec(
            name="replace-recovery",
            capabilities=("universe", "entry", "exit", "recovery"),
        )

    def select_market(self, market: Market) -> UniverseDecision:
        return UniverseDecision.include(reason="selected")

    def size_entry(self, context: ExtensionContext):  # pragma: no cover - not used in this test
        raise AssertionError("size_entry should not be called in reconcile replace test")

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:  # pragma: no cover - not used
        raise AssertionError("decide_entry should not be called in reconcile replace test")

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="no_missing_sell")

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        return RecoveryDecision(
            reason="replace_existing_sell",
            actions=(
                ExtensionDecision.replace(
                    reason="repriced",
                    token_id=None if context.market is None else _no_token_id(context.market),
                    order_id="sell-1",
                    price=Decimal("0.81"),
                    size_shares=Decimal("5"),
                    market_slug=context.market.market_slug if context.market is not None else None,
                ),
            ),
        )

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return ()


class _ReconcileHooks:
    @property
    def spec(self) -> ExtensionSpec:
        return ExtensionSpec(
            name="reconcile-hooks",
            capabilities=("universe", "entry", "exit", "recovery"),
        )

    def select_market(self, market: Market) -> UniverseDecision:
        return UniverseDecision.include(reason="selected")

    def size_entry(self, context: ExtensionContext) -> EntrySizing:  # pragma: no cover - not used
        raise AssertionError("size_entry should not be called in reconcile tests")

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:  # pragma: no cover - not used
        raise AssertionError("decide_entry should not be called in reconcile tests")

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="no_exit")

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        actions: list[ExtensionDecision] = []
        for order in context.open_orders:
            order_id = order.order_id or order.idempotency_key
            if order.side == OrderSide.BUY and order_id:
                actions.append(
                    ExtensionDecision.cancel(
                        reason="cancel_open_buy",
                        token_id=order.token_id,
                        order_id=order_id,
                        market_slug=order.market_slug,
                    )
                )
        for view in context.market_token_views:
            if view.position is None or view.position.shares <= Decimal("0"):
                continue
            if any(order.side == OrderSide.SELL for order in view.open_orders):
                continue
            actions.append(
                ExtensionDecision.sell(
                    reason="recover_missing_sell",
                    token_id=view.token_id,
                    price=Decimal("0.80"),
                    size_shares=view.position.shares,
                    order_type=OrderType.GTC,
                    market_slug=None if context.market is None else context.market.market_slug,
                )
            )
        return RecoveryDecision(reason="reconcile_requested", actions=tuple(actions))

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return ()

    def should_keep_tracking(self, market: Market, account_snapshot: object | None) -> bool:
        return True

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return existing_market.with_trading_status(candidate_market.trading_status, reject_reason=reason)


def _reconcile_service() -> ReconcileService:
    return ReconcileService(extension_hooks=_ReconcileHooks())


def test_reconcile_worker_resumes_stale_strategy_pause_when_market_recovers() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            category="Crypto",
            trading_status=TradingStatus.ELIGIBLE,
        )
        registry.upsert(market)

        account_state_store = AccountStateStore()
        account_state_store.pause_market("condition", reason="missing_primary_outcome")

        worker = ReconcileWorker(
            reconcile_service=_reconcile_service(),
            registry_snapshot_provider=registry.snapshot,
            account_state_store=account_state_store,
        )

        result = await worker.reconcile_once(trace_id="trace-resume-stale-pause")

        action_types = {action.action_type for action in result.plan.market_plans[0].actions}
        assert ReconcileActionType.RESUME_TRADING in action_types
        assert not account_state_store.snapshot().is_market_paused("condition")

    asyncio.run(run())


def test_reconcile_worker_resumes_stale_missing_primary_pause_with_current_strategy() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            category="Crypto",
            trading_status=TradingStatus.ELIGIBLE,
        )
        registry.upsert(market)

        account_state_store = AccountStateStore()
        account_state_store.pause_market("condition", reason="missing_primary_outcome")

        worker = ReconcileWorker(
            reconcile_service=ReconcileService(extension_hooks=build_strategy().hooks),
            registry_snapshot_provider=registry.snapshot,
            account_state_store=account_state_store,
        )

        result = await worker.reconcile_once(trace_id="trace-current-resume-stale-pause")

        market_plan = result.plan.market_plans[0]
        action_types = {action.action_type for action in market_plan.actions}
        assert market_plan.pause_trading is False
        assert ReconcileActionType.RESUME_TRADING in action_types
        assert not account_state_store.snapshot().is_market_paused("condition")

    asyncio.run(run())


class _StubGammaMarket:
    def __init__(self, market: Market) -> None:
        self.condition_id = market.condition_id
        self.market_slug = market.market_slug
        self.clob_enabled = True
        self._market = market

    def to_market(self) -> Market:
        return self._market


class _StubGammaClient:
    def __init__(self, market: Market) -> None:
        self._market = market

    async def list_markets(self, **kwargs: object) -> tuple[_StubGammaMarket, ...]:
        return (_StubGammaMarket(self._market),)


class _StubOrderbookDTO:
    def __init__(self, snapshot: OrderbookSnapshot) -> None:
        self._snapshot = snapshot

    def to_snapshot(self) -> OrderbookSnapshot:
        return self._snapshot


class _StubClobClient:
    has_auth_client = False

    def __init__(self, snapshot: OrderbookSnapshot, fee_rate_bps: int) -> None:
        self._snapshot = snapshot
        self._fee_rate_bps = fee_rate_bps
        self.fee_rate_calls = 0

    async def get_orderbook(self, *args: object, **kwargs: object) -> _StubOrderbookDTO:
        return _StubOrderbookDTO(self._snapshot)

    async def get_fee_rate(self, token_id: str) -> int:
        self.fee_rate_calls += 1
        return self._fee_rate_bps


class _StubPositionsDataClient:
    async def list_positions(self) -> tuple[Position, ...]:
        return ()


class _StubAccountClobClient:
    def __init__(self, *, balance_usdc: Decimal, allowance_usdc: Decimal) -> None:
        self._balance = balance_usdc
        self._allowance = allowance_usdc

    async def list_open_orders(self) -> tuple[OrderRecord, ...]:
        return ()

    async def list_fills(self) -> tuple[object, ...]:
        return ()

    async def get_balance_allowance(self) -> SimpleNamespace:
        return SimpleNamespace(
            balance_usdc=self._balance,
            allowance_usdc=self._allowance,
        )


class _SlowAccountClobClient:
    async def list_open_orders(self) -> tuple[OrderRecord, ...]:
        await asyncio.sleep(1)
        return ()

    async def list_fills(self) -> tuple[object, ...]:
        await asyncio.sleep(1)
        return ()

    async def get_balance_allowance(self) -> SimpleNamespace:
        await asyncio.sleep(1)
        return SimpleNamespace(
            balance_usdc=Decimal("0"),
            allowance_usdc=Decimal("0"),
        )


class _SlowPositionsDataClient:
    async def list_positions(self) -> tuple[Position, ...]:
        await asyncio.sleep(1)
        return ()


def test_reconcile_worker_cancels_open_buy_and_recovers_missing_sell() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            category="Crypto",
            trading_status=TradingStatus.ELIGIBLE,
        )
        registry.upsert(market)

        account_state_store = AccountStateStore()
        account_state_store.upsert_position(
            Position(
                condition_id="condition",
                token_id="token",
                market_slug=market.market_slug,
                shares=Decimal("5"),
                cost_usdc=Decimal("3"),
            )
        )
        account_state_store.upsert_order(
            OrderRecord(
                trace_id="trace-buy",
                condition_id="condition",
                token_id="token",
                market_slug=market.market_slug,
                side=OrderSide.BUY,
                order_type=OrderType.FAK,
                price=Decimal("0.43"),
                amount_usdc=Decimal("3"),
                order_id="buy-1",
                status=OrderStatus.LIVE,
                remaining_shares=Decimal("5"),
                idempotency_key="buy-1",
                reason="open_buy_detected",
            )
        )

        executor = _StubExecutor()
        worker = ReconcileWorker(
            reconcile_service=_reconcile_service(),
            registry_snapshot_provider=registry.snapshot,
            account_state_store=account_state_store,
            trading_service=TradingService(executor=executor),
        )

        result = await worker.reconcile_once(trace_id="trace-reconcile")

        assert result.plan.has_changes
        action_types = {action.action_type for action in result.plan.market_plans[0].actions}
        assert ReconcileActionType.CANCEL_ORDER in action_types
        assert ReconcileActionType.SUBMIT_ORDER in action_types

    asyncio.run(run())


def test_reconcile_worker_does_not_submit_after_trading_service_rejects() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            category="Crypto",
            trading_status=TradingStatus.ELIGIBLE,
        )
        registry.upsert(market)

        account_state_store = AccountStateStore()
        account_state_store.upsert_position(
            Position(
                condition_id="condition",
                token_id="token",
                market_slug=market.market_slug,
                shares=Decimal("5"),
                cost_usdc=Decimal("3"),
            )
        )

        worker = ReconcileWorker(
            reconcile_service=_reconcile_service(),
            registry_snapshot_provider=registry.snapshot,
            account_state_store=account_state_store,
            trading_service=_RejectingTradingService(),
        )

        result = await worker.reconcile_once(trace_id="trace-reconcile-rejected")

        assert result.plan.has_changes
        action_types = {action.action_type for action in result.plan.market_plans[0].actions}
        assert action_types == {ReconcileActionType.SUBMIT_ORDER}

        snapshot = account_state_store.snapshot()
        assert snapshot.open_sell_orders_for_market(market.condition_id, _no_token_id(market)) == ()
        position = snapshot.get_position(market.condition_id, _no_token_id(market))
        assert position is not None
        assert position.open_sell_shares == Decimal("0")

    asyncio.run(run())


def test_reconcile_worker_does_not_accept_executor_bypass() -> None:
    with pytest.raises(TypeError):
        ReconcileWorker(
            reconcile_service=_reconcile_service(),
            executor=_StubExecutor(),
        )


def test_reconcile_worker_refreshes_market_fee_fields() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        current_market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            category="Crypto",
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            trading_status=TradingStatus.ELIGIBLE,
        )
        registry.upsert(current_market)

        refreshed_market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            neg_risk=False,
            fees_enabled=True,
            maker_base_fee_bps=0,
            category="Crypto",
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            trading_status=TradingStatus.ELIGIBLE,
        )
        orderbook_snapshot = OrderbookSnapshot(
            token_id="token",
            best_bid=Decimal("0.55"),
            best_ask=Decimal("0.59"),
            bids=(PriceLevel(price=Decimal("0.55"), size=Decimal("100")),),
            asks=(PriceLevel(price=Decimal("0.59"), size=Decimal("200")),),
            received_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            market_slug="token-threshold-market",
            condition_id="condition",
            best_bid_size=Decimal("100"),
            best_ask_size=Decimal("200"),
            tick_size=Decimal("0.01"),
        )
        clob_client = _StubClobClient(orderbook_snapshot, fee_rate_bps=125)

        worker = ReconcileWorker(
            reconcile_service=_reconcile_service(),
            registry_snapshot_provider=registry.snapshot,
            registry=registry,
            gamma_client=_StubGammaClient(refreshed_market),
            clob_client=clob_client,
        )

        await worker.reconcile_once(trace_id="trace-reconcile")

        market = registry.get_by_condition_id("condition")
        assert market is not None
        assert market.fees_enabled is True
        assert market.maker_base_fee_bps == 0
        assert market.taker_base_fee_bps is None
        assert market.fee_rate_bps == 125
        assert market.fee_rate_updated_at is not None
        assert clob_client.fee_rate_calls == 1

        status = worker.status_snapshot()
        assert status.last_refresh_summary is not None
        assert status.last_refresh_summary.refreshed_fee_rates == 1

    asyncio.run(run())


def test_reconcile_worker_keeps_gamma_fee_schedule_without_fetching_fee_rate() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        current_market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            neg_risk=False,
            category="Crypto",
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            trading_status=TradingStatus.ELIGIBLE,
        )
        registry.upsert(current_market)

        refreshed_market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            neg_risk=False,
            fees_enabled=True,
            maker_base_fee_bps=0,
            taker_base_fee_bps=72,
            fee_rate_bps=72,
            category="Crypto",
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            trading_status=TradingStatus.ELIGIBLE,
        )
        orderbook_snapshot = OrderbookSnapshot(
            token_id="token",
            best_bid=Decimal("0.55"),
            best_ask=Decimal("0.59"),
            bids=(PriceLevel(price=Decimal("0.55"), size=Decimal("100")),),
            asks=(PriceLevel(price=Decimal("0.59"), size=Decimal("200")),),
            received_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            market_slug="token-threshold-market",
            condition_id="condition",
            best_bid_size=Decimal("100"),
            best_ask_size=Decimal("200"),
            tick_size=Decimal("0.01"),
        )
        clob_client = _StubClobClient(orderbook_snapshot, fee_rate_bps=1000)

        worker = ReconcileWorker(
            reconcile_service=_reconcile_service(),
            registry_snapshot_provider=registry.snapshot,
            registry=registry,
            gamma_client=_StubGammaClient(refreshed_market),
            clob_client=clob_client,
        )

        await worker.reconcile_once(trace_id="trace-reconcile")

        market = registry.get_by_condition_id("condition")
        assert market is not None
        assert market.taker_base_fee_bps == 72
        assert market.fee_rate_bps == 72
        assert clob_client.fee_rate_calls == 0

        status = worker.status_snapshot()
        assert status.last_refresh_summary is not None
        assert status.last_refresh_summary.refreshed_fee_rates == 0

    asyncio.run(run())


def test_reconcile_worker_executes_replace_requests_and_keeps_sell_coverage() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            category="Crypto",
            trading_status=TradingStatus.ELIGIBLE,
        )
        registry.upsert(market)

        account_state_store = AccountStateStore()
        account_state_store.upsert_position(
            Position(
                condition_id="condition",
                token_id="token",
                market_slug=market.market_slug,
                shares=Decimal("5"),
                cost_usdc=Decimal("3"),
                open_sell_shares=Decimal("5"),
            )
        )
        account_state_store.upsert_order(
            OrderRecord(
                trace_id="trace-sell",
                condition_id="condition",
                token_id="token",
                market_slug=market.market_slug,
                side=OrderSide.SELL,
                order_type=OrderType.GTC,
                price=Decimal("0.78"),
                size_shares=Decimal("5"),
                remaining_shares=Decimal("5"),
                notional_usdc=Decimal("3.5"),
                order_id="sell-1",
                status=OrderStatus.LIVE,
                idempotency_key="sell-1",
                reason="open_sell_detected",
            )
        )

        executor = _StubExecutor()
        worker = ReconcileWorker(
            reconcile_service=ReconcileService(extension_hooks=_ReplaceRecoveryStrategy()),
            registry_snapshot_provider=registry.snapshot,
            account_state_store=account_state_store,
            trading_service=TradingService(executor=executor),
        )

        result = await worker.reconcile_once(trace_id="trace-reconcile-replace")

        assert result.plan.has_changes
        action_types = {action.action_type for action in result.plan.market_plans[0].actions}
        assert action_types == {ReconcileActionType.REPLACE_ORDER}
        assert len(executor.replaced_intents) == 1
        replace_intent = executor.replaced_intents[0]
        assert replace_intent.order_id == "sell-1"
        assert replace_intent.new_price == Decimal("0.81")
        assert replace_intent.size_shares == Decimal("5")

        snapshot = account_state_store.snapshot()
        position = snapshot.get_position(market.condition_id, _no_token_id(market))
        assert position is not None
        assert position.open_sell_shares == Decimal("5")
        open_sell_orders = snapshot.open_sell_orders_for_market(market.condition_id, _no_token_id(market))
        assert len(open_sell_orders) == 1
        assert open_sell_orders[0].order_id == "sell-2"
        assert open_sell_orders[0].price == Decimal("0.81")
        assert open_sell_orders[0].remaining_shares == Decimal("5")

    asyncio.run(run())


def test_reconcile_authority_refresher_refreshes_account_balance_from_clob_balance_allowance() -> None:
    async def run() -> None:
        account_state_store = AccountStateStore()
        refresher = ReconcileAuthorityRefresher(
            account_state_store=account_state_store,
            data_client=_StubPositionsDataClient(),
            clob_client=_StubAccountClobClient(
                balance_usdc=Decimal("120"),
                allowance_usdc=Decimal("90"),
            ),
            trading_client=object(),
        )

        summary = await refresher.refresh_account(
            trace_id="trace-reconcile",
            markets=(),
        )

        snapshot = account_state_store.snapshot()
        assert snapshot.balance_usdc == Decimal("120")
        assert snapshot.allowance_usdc == Decimal("90")
        assert summary.refreshed_balance is True
        assert summary.refreshed_allowance is True

    asyncio.run(run())


def test_reconcile_authority_refresher_times_out_slow_account_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconcile_authority_refresher_module, "_AUTHORITY_CALL_TIMEOUT_S", 0.01)

    async def run() -> None:
        account_state_store = AccountStateStore()
        refresher = ReconcileAuthorityRefresher(
            account_state_store=account_state_store,
            data_client=_SlowPositionsDataClient(),
            clob_client=_SlowAccountClobClient(),
            trading_client=object(),
        )

        summary = await refresher.refresh_account(
            trace_id="trace-reconcile-timeout",
            markets=(),
        )

        assert summary.refreshed_positions == 0
        assert summary.refreshed_open_orders == 0
        assert summary.refreshed_fills == 0
        assert summary.refreshed_balance is False
        assert summary.refreshed_allowance is False
        assert {
            (failure.component, failure.operation, failure.reason)
            for failure in summary.failures
        } == {
            ("data", "positions", "timeout"),
            ("clob", "open_orders", "timeout"),
            ("clob", "fills", "timeout"),
            ("clob", "balance_allowance", "timeout"),
        }
        assert summary.as_payload()["failures"] == [
            {
                "component": "data",
                "operation": "positions",
                "target": None,
                "reason": "timeout",
                "detail": "",
                "retryable": True,
            },
            {
                "component": "clob",
                "operation": "open_orders",
                "target": None,
                "reason": "timeout",
                "detail": "",
                "retryable": True,
            },
            {
                "component": "clob",
                "operation": "fills",
                "target": None,
                "reason": "timeout",
                "detail": "",
                "retryable": True,
            },
            {
                "component": "clob",
                "operation": "balance_allowance",
                "target": None,
                "reason": "timeout",
                "detail": "",
                "retryable": True,
            },
        ]

    asyncio.run(run())


def test_reconcile_worker_prunes_out_of_universe_market_after_flattening() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            category="Crypto",
            trading_status=TradingStatus.PAUSED,
            reject_reason="market_out_of_universe",
        )
        registry.upsert(market)
        account_state_store = AccountStateStore()
        market_ws_worker = MarketWsWorker(registry=registry)
        market_ws_worker.track_market(market)

        worker = ReconcileWorker(
            reconcile_service=_reconcile_service(),
            registry_snapshot_provider=registry.snapshot,
            account_state_store=account_state_store,
            registry=registry,
            market_ws_worker=market_ws_worker,
        )

        await worker.reconcile_once(trace_id="trace-prune-filtered")

        assert registry.get_by_condition_id("condition") is None
        assert market_ws_worker.status_snapshot().tracked_token_ids == ()

    asyncio.run(run())


def test_reconcile_worker_emits_observe_events_without_requeueing_maintenance() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        market = build_binary_market(
            condition_id="condition",
            market_slug="token-threshold-market",
            no_token_id="token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            event_title="Will token reach a threshold?",
            market_question="Will this project hit the target threshold?",
            category="Crypto",
            trading_status=TradingStatus.ELIGIBLE,
        )
        registry.upsert(market)
        account_state_store = AccountStateStore()
        event_bus = EventBus()
        outbox = LocalOutbox(max_size=16)
        event_bus.bind_persistence_sink(build_domain_event_outbox_sink(outbox))

        worker = ReconcileWorker(
            event_bus=event_bus,
            reconcile_service=_reconcile_service(),
            registry_snapshot_provider=registry.snapshot,
            account_state_store=account_state_store,
            registry=registry,
        )

        await worker.reconcile_once(trace_id="trace-reconcile-observe-only")

        assert event_bus.snapshot().maintenance_queue_depth == 0
        assert event_bus.snapshot().persistence_queue_depth == 0
        assert len(outbox.pending_events()) == 0

    asyncio.run(run())
