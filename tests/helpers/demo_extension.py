from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import OrderType
from polymarket_trader.extension_api import (
    AccountSnapshotView,
    BusinessExtension,
    DiscoveryQuery,
    EntrySizing,
    ExtensionManifest,
    ExtensionSpec,
    RecoveryDecision,
    ExtensionContext,
    ExtensionDecision,
    ExtensionPorts,
    UniverseDecision,
)


class DemoExtension:
    def __init__(
        self,
        *,
        ports: ExtensionPorts | None = None,
        config_path: str | None = None,
    ) -> None:
        self.ports = ports
        self.config_path = config_path

    @property
    def spec(self) -> ExtensionSpec:
        return ExtensionSpec(
            name="demo",
            capabilities=("universe", "entry", "exit", "recovery"),
        )

    @property
    def hooks(self) -> "DemoExtension":
        return self

    def select_market(self, market: Market) -> UniverseDecision:
        return UniverseDecision.include(reason="demo_selected")

    def discovery_queries(self) -> tuple[DiscoveryQuery, ...]:
        return ()

    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        candidate = context.entry_candidates[0] if context.entry_candidates else None
        token_id = context.token_id or (None if candidate is None else candidate.token_id)
        market = context.market or (None if candidate is None else candidate.market)
        total_budget = context.portfolio_budget_usdc or Decimal("0")
        buy_budget = min(
            context.available_usdc or total_budget,
            context.max_order_usdc or total_budget,
            total_budget / Decimal("2"),
        )
        allocation = None
        if market is not None and token_id is not None and buy_budget > 0:
            allocation = Allocation(
                condition_id=market.condition_id,
                target_budget_usdc=buy_budget,
                buy_budget_usdc=buy_budget,
                market_slug=market.market_slug,
                token_id=token_id,
                reason="demo_entry_sized",
            )
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=total_budget,
                allocations=() if allocation is None else (allocation,),
                reason="demo_entry_sized",
            ),
            allocation=allocation,
            reason="demo_entry_sized",
        )

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:
        if context.orderbook is None or context.orderbook.best_ask is None:
            return ExtensionDecision.skip(reason="demo_missing_price")
        return ExtensionDecision.buy(
            reason="demo_entry",
            token_id=context.token_id,
            price=context.orderbook.best_ask,
            amount_usdc=context.amount_usdc or Decimal("0"),
            order_type=OrderType.FAK,
            market_slug=None if context.market is None else context.market.market_slug,
        )

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="demo_no_exit")

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        return RecoveryDecision(reason="demo_no_recovery")

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return ()

    def should_keep_tracking(
        self,
        market: Market,
        account_snapshot: AccountSnapshotView | None,
    ) -> bool:
        return True

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return existing_market.with_trading_status(candidate_market.trading_status, reject_reason=reason)


def build_extension(
    *,
    ports: ExtensionPorts | None = None,
    config_path: str | None = None,
) -> BusinessExtension:
    return DemoExtension(ports=ports, config_path=config_path)


manifest = ExtensionManifest(
    name="demo",
    version="1",
    module_path="tests.helpers.demo_extension",
    factory=build_extension,
)
