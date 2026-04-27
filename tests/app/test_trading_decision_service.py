from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.extension_api import (
    EntrySizing,
    ExtensionSpec,
    RecoveryDecision,
    ExtensionContext,
    ExtensionDecision,
    UniverseDecision,
)
from polymarket_trader.runtime.registry import MarketRegistry
from tests.helpers.markets import build_binary_market


class _CustomSizingStrategy:
    @property
    def spec(self) -> ExtensionSpec:
        return ExtensionSpec(
            name="custom",
            capabilities=("universe", "sizing", "entry", "exit", "recovery"),
        )

    def select_market(self, market: Market) -> UniverseDecision:
        return UniverseDecision.include(reason="selected")

    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        assert context.market is not None
        allocation = Allocation(
            condition_id=context.market.condition_id,
            target_budget_usdc=Decimal("33"),
            buy_budget_usdc=Decimal("33"),
            market_slug=context.market.market_slug,
            token_id=context.market.require_token_id("NO"),
            reason="custom_sizing",
        )
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=Decimal("100"),
                allocations=(allocation,),
                reason="custom_sizing",
            ),
            allocation=allocation,
            reason="custom_sizing",
        )

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:
        assert context.market is not None
        assert context.amount_usdc is not None
        return ExtensionDecision.buy(
            reason="custom_entry",
            token_id=context.token_id or context.market.require_token_id("NO"),
            price=Decimal("0.43"),
            amount_usdc=context.amount_usdc,
            market_slug=context.market.market_slug,
        )

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="noop_exit")

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        return RecoveryDecision(reason="noop_recovery")

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return ()


class _AvailableSizingStrategy(_CustomSizingStrategy):
    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        assert context.market is not None
        assert context.available_usdc is not None
        allocation = Allocation(
            condition_id=context.market.condition_id,
            target_budget_usdc=context.available_usdc,
            buy_budget_usdc=context.available_usdc,
            market_slug=context.market.market_slug,
            token_id=context.market.require_token_id("NO"),
            reason="available_sizing",
        )
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=context.portfolio_budget_usdc or Decimal("0"),
                allocations=(allocation,),
                reason="available_sizing",
            ),
            allocation=allocation,
            reason="available_sizing",
        )


def test_trading_decision_service_accepts_extension_hooks_name() -> None:
    hooks = _CustomSizingStrategy()
    service = TradingDecisionService(extension_hooks=hooks)

    assert service is not None


def test_trading_decision_service_uses_extension_sizing_policy() -> None:
    registry = MarketRegistry()
    primary = build_binary_market(
        condition_id="condition-1",
        market_slug="slug-1",
        no_token_id="no-1",
        yes_token_id="yes-1",
        category="Crypto",
        matched_keywords=("threshold", "target"),
        trading_status=TradingStatus.ELIGIBLE,
    )
    secondary = build_binary_market(
        condition_id="condition-2",
        market_slug="slug-2",
        no_token_id="no-2",
        yes_token_id="yes-2",
        category="Crypto",
        matched_keywords=("threshold", "target"),
        trading_status=TradingStatus.ELIGIBLE,
    )
    registry.upsert(primary)
    registry.upsert(secondary)
    snapshots = {
        primary.require_token_id("NO"): _snapshot(primary),
        secondary.require_token_id("NO"): _snapshot(secondary),
    }
    service = TradingDecisionService(
        extension_hooks=_CustomSizingStrategy(),
        registry=registry,
        orderbook_reader=snapshots.get,
    )

    plan = service.build_entry_plan(
        condition_id=primary.condition_id,
        token_id=primary.require_token_id("NO"),
        trace_id="trace-custom-sizing",
        portfolio_budget_usdc=Decimal("100"),
        available_usdc=Decimal("100"),
        max_order_usdc=Decimal("100"),
        max_market_usdc=Decimal("100"),
        max_total_usdc=Decimal("100"),
    )

    assert plan.ready_to_trade
    assert plan.allocation is not None
    assert plan.allocation.buy_budget_usdc == Decimal("33")
    assert plan.intent is not None
    assert plan.intent.amount_usdc == Decimal("33")
    assert plan.reason == "custom_sizing"


def test_trading_decision_service_uses_account_spendable_usdc_when_available_not_provided() -> None:
    registry = MarketRegistry()
    market = build_binary_market(
        condition_id="condition-1",
        market_slug="slug-1",
        no_token_id="no-1",
        yes_token_id="yes-1",
        category="Crypto",
        matched_keywords=("threshold", "target"),
        trading_status=TradingStatus.ELIGIBLE,
    )
    registry.upsert(market)
    service = TradingDecisionService(
        extension_hooks=_AvailableSizingStrategy(),
        registry=registry,
        orderbook_reader={market.require_token_id("NO"): _snapshot(market)}.get,
    )

    plan = service.build_entry_plan(
        condition_id=market.condition_id,
        token_id=market.require_token_id("NO"),
        trace_id="trace-available-sizing",
        portfolio_budget_usdc=Decimal("100"),
        account_snapshot=AccountSnapshot(
            balance_usdc=Decimal("100"),
            allowance_usdc=Decimal("20"),
            allow_new_entries=True,
        ),
        max_order_usdc=Decimal("100"),
        max_market_usdc=Decimal("100"),
        max_total_usdc=Decimal("100"),
    )

    assert plan.ready_to_trade
    assert plan.intent is not None
    assert plan.intent.amount_usdc == Decimal("20")


def _snapshot(market: Market) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=market.require_token_id("NO"),
        best_bid=Decimal("0.55"),
        best_ask=Decimal("0.43"),
        bids=(PriceLevel(price=Decimal("0.55"), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal("0.43"), size=Decimal("100")),),
        received_at=datetime.now(timezone.utc),
        market_slug=market.market_slug,
        condition_id=market.condition_id,
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("100"),
        tick_size=market.tick_size,
    )
