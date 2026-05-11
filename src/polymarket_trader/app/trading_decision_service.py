from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

from polymarket_trader.app.decision_recorder import (
    DecisionEventRecorder,
    build_decision_record_from_hook,
)
from polymarket_trader.app.extension_intent_builder import decision_to_managed_intent
from polymarket_trader.app.entry_plan import EntryPlan
from polymarket_trader.app.entry_planner import OrderbookReader, EntryPlanner
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import ManagedOrderIntent, Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api import ExtensionContext, ExtensionDecision, ExtensionHooks
from polymarket_trader.extension_api.manual_confirmation import ManualConfirmation
from polymarket_trader.runtime.registry import MarketRegistry


class TradingDecisionService:
    """Bridge extension hooks into framework plans and managed order intents."""

    def __init__(
        self,
        *,
        extension_hooks: ExtensionHooks,
        registry: MarketRegistry | None = None,
        orderbook_reader: OrderbookReader | None = None,
        decision_recorder: DecisionEventRecorder | None = None,
    ) -> None:
        self._extension_hooks = extension_hooks
        self._registry = registry
        self._orderbook_reader = orderbook_reader
        self._decision_recorder = decision_recorder
        self._entry_planner = EntryPlanner(
            extension_hooks=extension_hooks,
            registry=registry,
            orderbook_reader=orderbook_reader,
            decision_recorder=decision_recorder,
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
        manual_confirmation: ManualConfirmation | None = None,
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
            manual_confirmation=manual_confirmation,
        )

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        decisions = self._extension_hooks.decide_follow_up(context)
        self._record(hook_name="decide_follow_up", context=context, decision=decisions)
        return decisions

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        """根据当前热态持仓生成退出决策。

        该方法只桥接策略 hook，不直接解释具体策略字段；调用侧仍需把
        返回的决策转换为受控 intent，并统一经过 TradingService/RiskManager。
        """

        decision = self._extension_hooks.decide_exit(context)
        self._record(hook_name="decide_exit", context=context, decision=decision)
        return decision

    def _record(
        self,
        *,
        hook_name: str,
        context: ExtensionContext,
        decision: object,
    ) -> None:
        if self._decision_recorder is None:
            return
        record = build_decision_record_from_hook(
            hook_name=hook_name,
            trace_id=context.trace_id,
            context=context,
            decision=decision,
            condition_id=context.market.condition_id if context.market is not None else None,
            token_id=context.token_id,
            market_slug=context.market.market_slug if context.market is not None else None,
        )
        if record is None:
            return
        # DecisionEventRecorder.record 内部把异常吞掉并仅做 outbox.put_nowait，
        # 决策返回不会被反向阻塞，这里不再额外 try/except。
        self._decision_recorder.record(record)

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
