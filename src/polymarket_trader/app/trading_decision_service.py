from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

from polymarket_trader.app.extension_intent_builder import decision_to_managed_intent
from polymarket_trader.app.entry_plan import EntryPlan
from polymarket_trader.app.entry_planner import OrderbookReader, EntryPlanner
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import ManagedOrderIntent, Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api import ExtensionContext, ExtensionDecision, ExtensionHooks
from polymarket_trader.runtime.registry import MarketRegistry


class TradingDecisionService:
    """Bridge extension hooks into framework plans and managed order intents."""

    def __init__(
        self,
        *,
        extension_hooks: ExtensionHooks,
        registry: MarketRegistry | None = None,
        orderbook_reader: OrderbookReader | None = None,
    ) -> None:
        self._extension_hooks = extension_hooks
        self._registry = registry
        self._orderbook_reader = orderbook_reader
        self._entry_planner = EntryPlanner(
            extension_hooks=extension_hooks,
            registry=registry,
            orderbook_reader=orderbook_reader,
        )

    def build_entry_plan(
        self,
        *,
        market: Market | None = None,
        orderbook: OrderbookSnapshot | None = None,
        account_snapshot: AccountSnapshot | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        trace_id: str | None = None,
        portfolio_budget_usdc: Decimal,
        available_usdc: Decimal | None = None,
        max_order_usdc: Decimal,
        max_market_usdc: Decimal,
        max_total_usdc: Decimal,
        positions: Iterable[Position] = (),
        open_orders: Iterable[Order] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> EntryPlan:
        return self._entry_planner.build_entry_plan(
            market=market,
            orderbook=orderbook,
            account_snapshot=account_snapshot,
            condition_id=condition_id,
            token_id=token_id,
            trace_id=trace_id,
            portfolio_budget_usdc=portfolio_budget_usdc,
            available_usdc=available_usdc,
            max_order_usdc=max_order_usdc,
            max_market_usdc=max_market_usdc,
            max_total_usdc=max_total_usdc,
            positions=positions,
            open_orders=open_orders,
            metadata=metadata,
        )

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return self._extension_hooks.decide_follow_up(context)

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        """根据当前热态持仓生成退出决策。

        该方法只桥接策略 hook，不直接解释具体策略字段；调用侧仍需把
        返回的决策转换为受控 intent，并统一经过 TradingService/RiskManager。
        """

        return self._extension_hooks.decide_exit(context)

    def resolve_market(
        self,
        *,
        condition_id: str | None,
        token_id: str | None,
    ) -> Market | None:
        if self._registry is None:
            return None
        if condition_id is not None:
            market = self._registry.get_by_condition_id(condition_id)
            if market is not None:
                return market
        if token_id is not None:
            return self._registry.get_by_token_id(token_id)
        return None

    def lookup_orderbook(self, token_id: str) -> OrderbookSnapshot | None:
        if self._orderbook_reader is None:
            return None
        return self._orderbook_reader(token_id)

    def build_intent_from_decision(
        self,
        *,
        trace_id: str,
        condition_id: str,
        market_slug: str | None,
        default_token_id: str | None,
        decision: ExtensionDecision,
    ) -> ManagedOrderIntent | None:
        return decision_to_managed_intent(
            trace_id=trace_id,
            condition_id=condition_id,
            market_slug=market_slug,
            default_token_id=default_token_id,
            decision=decision,
        )
