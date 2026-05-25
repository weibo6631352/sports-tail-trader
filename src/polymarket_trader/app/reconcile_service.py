from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from polymarket_trader.quant.strategy import CurrentStrategy

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, Mapping
from uuid import uuid4

from polymarket_trader.app.extension_intent_builder import decision_to_managed_intent
from polymarket_trader.app.order_projection import normalize_order_id
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    Order,
    OrderSide,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.account import AccountSnapshot, MarketPause
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.domain.decisions import DecisionContext, MarketTokenView, TradeAction, TradingDecision
from polymarket_trader.serialization import utc_now


class ReconcileActionType(StrEnum):
    CANCEL_ORDER = "cancel_order"
    SUBMIT_ORDER = "submit_order"
    REPLACE_ORDER = "replace_order"
    PAUSE_TRADING = "pause_trading"
    RESUME_TRADING = "resume_trading"


def _order_open_size(order: Order) -> Decimal:
    if order.remaining_shares is not None:
        return max(order.remaining_shares, Decimal("0"))
    if order.size_shares is not None:
        return max(order.size_shares, Decimal("0"))
    if order.amount_usdc is not None:
        return max(order.amount_usdc, Decimal("0"))
    return Decimal("0")


@dataclass(frozen=True, slots=True)
class ReconcileAction:
    action_type: ReconcileActionType
    trace_id: str
    condition_id: str
    token_id: str | None
    market_slug: str | None
    reason: str
    source_order_id: str | None = None
    source_order_side: OrderSide | None = None
    target_size_shares: Decimal | None = None
    target_notional_usdc: Decimal | None = None
    pause_reason: str | None = None
    intent: BuyOrderIntent | SellOrderIntent | CancelOrderIntent | ReplaceOrderIntent | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def priority(self) -> int:
        return 1 if self.action_type in {ReconcileActionType.PAUSE_TRADING, ReconcileActionType.RESUME_TRADING} else 2

    @property
    def merge_key(self) -> str:
        return "|".join(
            (
                self.action_type.value,
                self.condition_id,
                self.token_id or "",
                self.source_order_id or "",
                self.reason,
            )
        )


@dataclass(frozen=True, slots=True)
class ReconcileMarketPlan:
    trace_id: str
    market: Market
    position: Position | None
    open_orders: tuple[Order, ...]
    actions: tuple[ReconcileAction, ...]
    pause_trading: bool
    pause_reason: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.actions) or self.pause_trading


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    trace_id: str
    generated_at: datetime
    market_plans: tuple[ReconcileMarketPlan, ...]
    total_actions: int
    paused_market_count: int

    @property
    def diff_count(self) -> int:
        return self.total_actions

    @property
    def has_changes(self) -> bool:
        return self.total_actions > 0 or self.paused_market_count > 0


class ReconcileService:
    """Builds strategy-driven reconcile diffs from hot snapshots."""

    def __init__(
        self,
        *,
        strategy: "CurrentStrategy",
        strategy_id: str,
        entry_metadata_provider: Callable[[Market], Mapping[str, Any]] | None = None,
        orderbook_reader: Callable[[str], OrderbookSnapshot | None] | None = None,
    ) -> None:
        if not strategy_id:
            raise ValueError("ReconcileService requires non-empty strategy_id")
        self._extension_hooks = strategy
        self._strategy_id = strategy_id
        self._entry_metadata_provider = entry_metadata_provider
        self._orderbook_reader = orderbook_reader

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    def build_reconcile_plan(
        self,
        *,
        registry_snapshot: MarketRegistrySnapshot,
        account_snapshot: AccountSnapshot,
        trace_id: str | None = None,
        condition_ids: tuple[str, ...] | None = None,
    ) -> ReconcilePlan:
        trace_id = trace_id or uuid4().hex
        markets = registry_snapshot.markets
        if condition_ids is not None:
            condition_id_set = set(condition_ids)
            markets = tuple(market for market in markets if market.condition_id in condition_id_set)

        market_plans = tuple(
            self.build_market_plan(
                market=market,
                account_snapshot=account_snapshot,
                trace_id=trace_id,
            )
            for market in markets
        )
        total_actions = sum(len(plan.actions) for plan in market_plans)
        paused_market_count = sum(1 for plan in market_plans if plan.pause_trading)
        return ReconcilePlan(
            trace_id=trace_id,
            generated_at=utc_now(),
            market_plans=market_plans,
            total_actions=total_actions,
            paused_market_count=paused_market_count,
        )

    def build_market_plan(
        self,
        *,
        market: Market,
        account_snapshot: AccountSnapshot,
        trace_id: str | None = None,
    ) -> ReconcileMarketPlan:
        trace_id = trace_id or uuid4().hex
        # Reconcile plan 需要携带当前 market 的主持仓，供审计、展示和后续动作解释使用。
        positions = tuple(
            position
            for outcome in market.outcomes
            if (position := account_snapshot.get_position(market.condition_id, outcome.token_id)) is not None
        )
        position = positions[0] if positions else None
        open_orders = tuple(
            order
            for token_id in market.token_ids
            for order in account_snapshot.open_orders_for_market(market.condition_id, token_id)
        )
        market_token_views = tuple(
            MarketTokenView(
                token_id=outcome.token_id,
                outcome=outcome.outcome,
                orderbook=(
                    None
                    if self._orderbook_reader is None
                    else self._orderbook_reader(outcome.token_id)
                ),
                position=account_snapshot.get_position(market.condition_id, outcome.token_id),
                open_orders=account_snapshot.open_orders_for_market(
                    market.condition_id,
                    outcome.token_id,
                ),
            )
            for outcome in market.outcomes
        )
        account_pause = account_snapshot.pause_for_market(market.condition_id)
        recovery_account_snapshot = _account_snapshot_for_recovery(
            account_snapshot,
            condition_id=market.condition_id,
            account_pause=account_pause,
        )
        metadata = self._metadata_for_market(market)
        recovery = self._extension_hooks.quant_decide(
            DecisionContext(
                trace_id=trace_id,
                strategy_id=self._strategy_id,
                market=market,
                market_token_views=market_token_views,
                account_snapshot=recovery_account_snapshot,
                position=position,
                open_orders=open_orders,
                now=utc_now(),
                quant_trigger_kind="reconcile_cycle",
                metadata=metadata,
            )
        )
        recovery_decisions = list(recovery.actions)
        recovery_exit_tokens = {
            decision.token_id
            for decision in recovery_decisions
            if decision.action == TradeAction.SELL and decision.token_id is not None
        }
        for decision in self._position_exit_decisions(
            trace_id=trace_id,
            market=market,
            account_snapshot=account_snapshot,
            market_token_views=market_token_views,
            metadata=metadata,
        ):
            if decision.token_id in recovery_exit_tokens:
                continue
            recovery_decisions.append(decision)

        sticky_account_pause = account_pause is not None and not account_pause.recoverable
        pause_trading = (
            market.trading_status in {TradingStatus.PAUSED, TradingStatus.CLOSED, TradingStatus.RESOLVED}
            or sticky_account_pause
            or recovery.pause_trading
        )
        pause_reason = _resolved_pause_reason(
            market=market,
            account_snapshot=account_snapshot,
            account_pause=account_pause,
            sticky_account_pause=sticky_account_pause,
            recovery_pause_reason=recovery.pause_reason,
            pause_trading=pause_trading,
        )

        order_index = {
            normalize_order_id(order): order
            for order in open_orders
        }
        actions: list[ReconcileAction] = []
        if account_pause is not None and not pause_trading:
            actions.append(
                ReconcileAction(
                    action_type=ReconcileActionType.RESUME_TRADING,
                    trace_id=trace_id,
                    condition_id=market.condition_id,
                    token_id=None,
                    market_slug=market.market_slug,
                    reason="stale_pause_cleared",
                    pause_reason=account_pause.reason,
                )
            )
        for decision in recovery_decisions:
            intent = decision_to_managed_intent(
                trace_id=trace_id,
                strategy_id=self._strategy_id,
                condition_id=market.condition_id,
                market_slug=market.market_slug,
                default_token_id=None,
                decision=decision,
            )
            if intent is None:
                continue
            action = _action_from_intent(
                trace_id=trace_id,
                market=market,
                intent=intent,
                reason=decision.reason,
                order_index=order_index,
                metadata=decision.metadata,
            )
            actions.append(action)

        if pause_trading:
            actions.append(
                ReconcileAction(
                    action_type=ReconcileActionType.PAUSE_TRADING,
                    trace_id=trace_id,
                    condition_id=market.condition_id,
                    token_id=None,
                    market_slug=market.market_slug,
                    reason="market_not_tradable",
                    pause_reason=pause_reason,
                )
            )

        return ReconcileMarketPlan(
            trace_id=trace_id,
            market=market,
            position=position,
            open_orders=open_orders,
            actions=tuple(actions),
            pause_trading=pause_trading,
            pause_reason=pause_reason,
        )

    def _metadata_for_market(self, market: Market) -> Mapping[str, Any]:
        if self._entry_metadata_provider is None:
            return {}
        return dict(self._entry_metadata_provider(market))

    def _position_exit_decisions(
        self,
        *,
        trace_id: str,
        market: Market,
        account_snapshot: AccountSnapshot,
        market_token_views: tuple[MarketTokenView, ...],
        metadata: Mapping[str, Any],
    ) -> tuple[TradingDecision, ...]:
        """为已有未覆盖持仓补充退出决策。

        recovery 可以因为比赛结束、状态异常或 market 暂停而拒绝新入场；
        但已有仓位的退出保护不能被“没有 live”阻断。这里仍只调用策略
        ``decide_exit``，不在 app 层写具体策略价格或仓位规则。
        """

        decisions: list[TradingDecision] = []
        for outcome in market.outcomes:
            position = account_snapshot.get_position(market.condition_id, outcome.token_id)
            if position is None or position.shares <= Decimal("0"):
                continue
            open_orders = account_snapshot.open_orders_for_market(market.condition_id, outcome.token_id)
            open_sell_shares = account_snapshot.open_sell_shares_for_market(
                market.condition_id,
                outcome.token_id,
            )
            adjusted_position = position.with_open_sell_shares(
                max(position.open_sell_shares, open_sell_shares)
            )
            quant_decision = self._extension_hooks.quant_decide(
                DecisionContext(
                    trace_id=trace_id,
                    strategy_id=self._strategy_id,
                    market=market,
                    token_id=outcome.token_id,
                    market_token_views=market_token_views,
                    account_snapshot=account_snapshot,
                    position=adjusted_position,
                    open_orders=open_orders,
                    quant_trigger_kind="market_tick",
                    metadata={
                        **dict(metadata),
                        "exit_trigger": "reconcile_position",
                    },
                )
            )
            for decision in quant_decision.actions:
                if decision.action in {TradeAction.SELL, TradeAction.REPLACE}:
                    decisions.append(decision)
        return tuple(decisions)


def _action_from_intent(
    *,
    trace_id: str,
    market: Market,
    intent: BuyOrderIntent | SellOrderIntent | CancelOrderIntent | ReplaceOrderIntent,
    reason: str,
    order_index: dict[str, Order],
    metadata: Mapping[str, object],
) -> ReconcileAction:
    if isinstance(intent, CancelOrderIntent):
        source = order_index.get(intent.order_id)
        return ReconcileAction(
            action_type=ReconcileActionType.CANCEL_ORDER,
            trace_id=trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            reason=reason,
            source_order_id=intent.order_id,
            source_order_side=None if source is None else source.side,
            target_size_shares=None if source is None else _order_open_size(source),
            intent=intent,
            metadata=metadata,
        )
    if isinstance(intent, ReplaceOrderIntent):
        source = order_index.get(intent.order_id)
        return ReconcileAction(
            action_type=ReconcileActionType.REPLACE_ORDER,
            trace_id=trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            reason=reason,
            source_order_id=intent.order_id,
            source_order_side=None if source is None else source.side,
            target_size_shares=intent.size_shares,
            target_notional_usdc=intent.size_shares * intent.new_price,
            intent=intent,
            metadata=metadata,
        )
    target_notional_usdc = intent.amount_usdc
    if target_notional_usdc is None:
        size_shares = intent.size_shares
        if size_shares is not None and intent.price:
            target_notional_usdc = intent.price * size_shares
    return ReconcileAction(
        action_type=ReconcileActionType.SUBMIT_ORDER,
        trace_id=trace_id,
        condition_id=intent.condition_id,
        token_id=intent.token_id,
        market_slug=intent.market_slug or market.market_slug,
        reason=reason,
        source_order_side=intent.side,
        target_size_shares=intent.size_shares,
        target_notional_usdc=target_notional_usdc,
        intent=intent,
        metadata=metadata,
    )


def _pause_reason(market: Market, account_snapshot: AccountSnapshot) -> str:
    if market.trading_status == TradingStatus.PAUSED:
        return market.reject_reason or "market_paused"
    if market.trading_status == TradingStatus.CLOSED:
        return market.reject_reason or "market_closed"
    if market.trading_status == TradingStatus.RESOLVED:
        return market.reject_reason or "market_resolved"
    account_pause = account_snapshot.pause_for_market(market.condition_id)
    if account_pause is not None:
        return account_pause.reason
    return ""


def _account_snapshot_for_recovery(
    account_snapshot: AccountSnapshot,
    *,
    condition_id: str,
    account_pause: MarketPause | None,
) -> AccountSnapshot:
    if account_pause is None or not account_pause.recoverable:
        return account_snapshot
    return account_snapshot.without_market_pause(condition_id)


def _resolved_pause_reason(
    *,
    market: Market,
    account_snapshot: AccountSnapshot,
    account_pause: MarketPause | None,
    sticky_account_pause: bool,
    recovery_pause_reason: str | None,
    pause_trading: bool,
) -> str:
    if sticky_account_pause and account_pause is not None:
        return account_pause.reason
    if recovery_pause_reason:
        return recovery_pause_reason
    if pause_trading:
        return _pause_reason(market, account_snapshot)
    return ""
