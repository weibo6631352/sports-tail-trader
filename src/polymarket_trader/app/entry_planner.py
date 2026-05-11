from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

from polymarket_trader.app.extension_intent_builder import decision_to_trade_intent
from polymarket_trader.app.entry_plan import EntryPlan
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api import (
    EntryCandidate,
    ExtensionContext,
    ExtensionHooks,
    MarketTokenView,
)
from polymarket_trader.app.decision_recorder import (
    DecisionEventRecorder,
    build_decision_record_from_hook,
)
from polymarket_trader.extension_api.manual_confirmation import ManualConfirmation
from polymarket_trader.extension_api.summary import StrategySummary
from polymarket_trader.observability.trace import ensure_trace_id
from polymarket_trader.runtime.registry import MarketRegistry

OrderbookReader = Callable[[str], OrderbookSnapshot | None]


class EntryPlanner:
    def __init__(
        self,
        *,
        extension_hooks: ExtensionHooks,
        strategy_id: str,
        registry: MarketRegistry | None = None,
        orderbook_reader: OrderbookReader | None = None,
        decision_recorder: DecisionEventRecorder | None = None,
    ) -> None:
        if not strategy_id:
            raise ValueError("EntryPlanner requires non-empty strategy_id")
        self._extension_hooks = extension_hooks
        self._strategy_id = strategy_id
        self._registry = registry
        self._orderbook_reader = orderbook_reader
        self._decision_recorder = decision_recorder

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

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
        trace_id = trace_id or ensure_trace_id()
        base_metadata = dict(metadata or {})
        available_usdc, positions, open_orders = _entry_account_inputs(
            account_snapshot=account_snapshot,
            available_usdc=available_usdc,
            positions=positions,
            open_orders=open_orders,
        )
        resolved_market = market or self._resolve_market(condition_id=condition_id, token_id=token_id)
        resolved_token_id = token_id or (orderbook.token_id if orderbook is not None else None)
        resolved_orderbook = orderbook or self._resolve_orderbook(
            market=resolved_market,
            token_id=resolved_token_id,
        )
        if resolved_market is None or resolved_orderbook is None:
            return _unavailable_entry_plan(
                trace_id=trace_id,
                market=resolved_market,
                orderbook=resolved_orderbook,
                portfolio_budget_usdc=portfolio_budget_usdc,
                reason="missing_market_state",
                metadata=base_metadata,
            )

        if account_snapshot is not None:
            if not account_snapshot.allow_new_entries or account_snapshot.is_market_paused(
                resolved_market.condition_id
            ):
                return _unavailable_entry_plan(
                    trace_id=trace_id,
                    market=resolved_market,
                    orderbook=resolved_orderbook,
                    portfolio_budget_usdc=portfolio_budget_usdc,
                    reason="entry_paused",
                    metadata=base_metadata,
                )

        positions = tuple(positions)
        open_orders = tuple(open_orders)
        position_index = {
            (position.condition_id, position.token_id): position for position in positions
        }
        focus_token_id = resolved_token_id or resolved_orderbook.token_id
        entry_candidates = self._build_entry_candidates(
            trace_id=trace_id,
            focus_market=resolved_market,
            focus_token_id=focus_token_id,
            focus_orderbook=resolved_orderbook,
            position_index=position_index,
            open_orders=open_orders,
        ) or (
            self._build_entry_candidate(
                trace_id=trace_id,
                market=resolved_market,
                token_id=focus_token_id,
                orderbook=resolved_orderbook,
                position=position_index.get((resolved_market.condition_id, focus_token_id)),
                open_orders=_open_orders_for(open_orders, resolved_market.condition_id, focus_token_id),
            ),
        )

        sizing = self._extension_hooks.size_entry(
            self._sizing_context(
                trace_id=trace_id,
                market=resolved_market,
                token_id=resolved_token_id,
                orderbook=resolved_orderbook,
                account_snapshot=account_snapshot,
                position=position_index.get((resolved_market.condition_id, resolved_token_id or "")),
                open_orders=_open_orders_for(open_orders, resolved_market.condition_id, resolved_token_id or ""),
                entry_candidates=entry_candidates,
                portfolio_budget_usdc=portfolio_budget_usdc,
                available_usdc=available_usdc,
                max_order_usdc=max_order_usdc,
                max_market_usdc=max_market_usdc,
                max_total_usdc=max_total_usdc,
                metadata=base_metadata,
                manual_confirmation=manual_confirmation,
            )
        )
        plan = sizing.allocation_plan
        allocation = sizing.allocation or _pick_allocation(
            plan.allocations,
            resolved_market.condition_id,
            focus_token_id,
        )
        reason = sizing.reason or plan.reason
        intent = None
        decision_kind = None
        summary = None
        plan_metadata: dict[str, Any] = dict(base_metadata)
        plan_metadata.update(sizing.metadata or {})

        if allocation is not None:
            focus_token_id = allocation.token_id or focus_token_id
            reason = allocation.reason or reason
            if allocation.buy_budget_usdc > Decimal("0"):
                entry_context = self._entry_decision_context(
                    trace_id=trace_id,
                    market=resolved_market,
                    token_id=allocation.token_id or resolved_token_id,
                    orderbook=resolved_orderbook,
                    account_snapshot=account_snapshot,
                    position=position_index.get((resolved_market.condition_id, focus_token_id)),
                    open_orders=_open_orders_for(open_orders, resolved_market.condition_id, focus_token_id),
                    portfolio_budget_usdc=portfolio_budget_usdc,
                    available_usdc=available_usdc,
                    max_order_usdc=max_order_usdc,
                    max_market_usdc=max_market_usdc,
                    max_total_usdc=max_total_usdc,
                    allocation_plan=plan,
                    allocation=allocation,
                    metadata=base_metadata,
                    manual_confirmation=manual_confirmation,
                )
                decision = self._extension_hooks.decide_entry(entry_context)
                self._record_decision(
                    hook_name="decide_entry",
                    context=entry_context,
                    decision=decision,
                )
                plan_metadata.update(decision.metadata)
                decision_kind = decision.decision_kind
                summary = decision.summary
                intent = decision_to_trade_intent(
                    trace_id=trace_id,
                    strategy_id=self._strategy_id,
                    market=resolved_market,
                    default_token_id=focus_token_id,
                    decision=decision,
                )
                if intent is None and decision.reason:
                    reason = decision.reason

        # allocation 阶段被拒（无 allocation 或 buy_budget <= 0），decide_entry 没被调用
        # → summary 此时为 None，但策略 size_entry 返回的 metadata（sizing.metadata）
        # 含 market_family/market_type 等诊断信息，把它们投到 summary.extras 让
        # admin/virtual_paper 能看到非 single_game 早期拒绝的可观测性。
        if summary is None and (sizing.metadata or reason):
            summary = _build_unavailable_summary(
                reason=reason,
                sizing_extras=sizing.metadata,
            )

        return EntryPlan(
            trace_id=trace_id,
            market=resolved_market,
            orderbook=resolved_orderbook,
            allocation_plan=plan,
            allocation=allocation,
            intent=intent,
            eligible_market_count=plan.eligible_market_count,
            reason=reason,
            decision_kind=decision_kind,
            summary=summary,
            metadata=plan_metadata,
        )

    def _sizing_context(
        self,
        *,
        trace_id: str,
        market: Market,
        token_id: str | None,
        orderbook: OrderbookSnapshot,
        account_snapshot: AccountSnapshot | None,
        position: Position | None,
        open_orders: tuple[Order, ...],
        entry_candidates: tuple[EntryCandidate, ...],
        portfolio_budget_usdc: Decimal,
        available_usdc: Decimal | None,
        max_order_usdc: Decimal,
        max_market_usdc: Decimal,
        max_total_usdc: Decimal,
        metadata: Mapping[str, Any],
        manual_confirmation: ManualConfirmation | None = None,
    ) -> ExtensionContext:
        effective_available_usdc = available_usdc if available_usdc is not None else portfolio_budget_usdc
        context_metadata: dict[str, Any] = dict(metadata)
        context_metadata.update(
            {
                "portfolio_budget_usdc": portfolio_budget_usdc,
                "available_usdc": effective_available_usdc,
                "max_order_usdc": max_order_usdc,
                "max_market_usdc": max_market_usdc,
                "max_total_usdc": max_total_usdc,
            }
        )
        return ExtensionContext(
            trace_id=trace_id,
            strategy_id=self._strategy_id,
            market=market,
            token_id=token_id,
            orderbook=orderbook,
            market_token_views=_market_token_views(
                market,
                orderbook_reader=self._orderbook_reader,
                account_snapshot=account_snapshot,
            ),
            account_snapshot=account_snapshot,
            position=position,
            open_orders=open_orders,
            entry_candidates=entry_candidates,
            now=orderbook.received_at,
            portfolio_budget_usdc=portfolio_budget_usdc,
            available_usdc=effective_available_usdc,
            max_order_usdc=max_order_usdc,
            max_market_usdc=max_market_usdc,
            max_total_usdc=max_total_usdc,
            manual_confirmation=manual_confirmation,
            metadata=context_metadata,
        )

    def _entry_decision_context(
        self,
        *,
        trace_id: str,
        market: Market,
        token_id: str | None,
        orderbook: OrderbookSnapshot,
        account_snapshot: AccountSnapshot | None,
        position: Position | None,
        open_orders: tuple[Order, ...],
        portfolio_budget_usdc: Decimal,
        available_usdc: Decimal | None,
        max_order_usdc: Decimal,
        max_market_usdc: Decimal,
        max_total_usdc: Decimal,
        allocation_plan: AllocationPlan,
        allocation: Allocation,
        metadata: Mapping[str, Any],
        manual_confirmation: ManualConfirmation | None = None,
    ) -> ExtensionContext:
        context_metadata: dict[str, Any] = dict(metadata)
        context_metadata.update(
            {
                "allocation": allocation,
                "allocation_plan": allocation_plan,
                "amount_usdc": allocation.buy_budget_usdc,
                "buy_budget_usdc": allocation.buy_budget_usdc,
                "portfolio_budget_usdc": portfolio_budget_usdc,
                "available_usdc": available_usdc,
                "max_order_usdc": max_order_usdc,
                "max_market_usdc": max_market_usdc,
                "max_total_usdc": max_total_usdc,
            }
        )
        return ExtensionContext(
            trace_id=trace_id,
            strategy_id=self._strategy_id,
            market=market,
            token_id=token_id,
            orderbook=orderbook,
            market_token_views=_market_token_views(
                market,
                orderbook_reader=self._orderbook_reader,
                account_snapshot=account_snapshot,
            ),
            account_snapshot=account_snapshot,
            position=position,
            open_orders=open_orders,
            now=orderbook.received_at,
            portfolio_budget_usdc=portfolio_budget_usdc,
            available_usdc=available_usdc,
            max_order_usdc=max_order_usdc,
            max_market_usdc=max_market_usdc,
            max_total_usdc=max_total_usdc,
            allocation_plan=allocation_plan,
            allocation=allocation,
            amount_usdc=allocation.buy_budget_usdc,
            manual_confirmation=manual_confirmation,
            metadata=context_metadata,
        )

    def _build_entry_candidates(
        self,
        *,
        trace_id: str,
        focus_market: Market,
        focus_token_id: str,
        focus_orderbook: OrderbookSnapshot,
        position_index: dict[tuple[str, str], Position],
        open_orders: tuple[Order, ...],
    ) -> tuple[EntryCandidate, ...]:
        # 入场计划运行在 P0 交易热路径。每个 orderbook 事件只能围绕当前触发 market
        # 构造候选，不能为了预算分配枚举全量 registry 并读取所有盘口热态。
        markets = (focus_market,)
        candidates: list[EntryCandidate] = []
        for candidate in markets:
            for candidate_token_id in _candidate_token_ids(candidate):
                candidate_orderbook = (
                    focus_orderbook
                    if candidate.condition_id == focus_market.condition_id
                    and candidate_token_id == focus_token_id
                    else self._lookup_orderbook(candidate_token_id)
                )
                if candidate_orderbook is None:
                    continue
                candidates.append(
                    self._build_entry_candidate(
                        trace_id=trace_id,
                        market=candidate,
                        token_id=candidate_token_id,
                        orderbook=candidate_orderbook,
                        position=position_index.get((candidate.condition_id, candidate_token_id)),
                        open_orders=_open_orders_for(
                            open_orders,
                            candidate.condition_id,
                            candidate_token_id,
                        ),
                    )
                )
        return tuple(candidates)

    def _build_entry_candidate(
        self,
        *,
        trace_id: str,
        market: Market,
        token_id: str,
        orderbook: OrderbookSnapshot,
        position: Position | None,
        open_orders: tuple[Order, ...],
    ) -> EntryCandidate:
        return EntryCandidate(
            market=market,
            token_id=token_id,
            orderbook=orderbook,
            position=position,
            open_orders=open_orders,
            idempotency_key=f"{trace_id}:{market.condition_id}:{token_id}",
        )

    def _resolve_market(
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

    def _resolve_orderbook(
        self,
        *,
        market: Market | None,
        token_id: str | None,
    ) -> OrderbookSnapshot | None:
        if token_id is None:
            return None
        return self._lookup_orderbook(token_id)

    def _lookup_orderbook(self, token_id: str) -> OrderbookSnapshot | None:
        if self._orderbook_reader is None:
            return None
        return self._orderbook_reader(token_id)

    def _record_decision(
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
            strategy_id=context.strategy_id,
            context=context,
            decision=decision,
            condition_id=context.market.condition_id if context.market is not None else None,
            token_id=context.token_id,
            market_slug=context.market.market_slug if context.market is not None else None,
        )
        if record is None:
            return
        # DecisionEventRecorder.record 内部已经吞掉所有异常并仅做 outbox.put_nowait，
        # 不会反向阻塞决策返回；这里不再额外 try/except。
        self._decision_recorder.record(record)


def _entry_account_inputs(
    *,
    account_snapshot: AccountSnapshot | None,
    available_usdc: Decimal | None,
    positions: Iterable[Position],
    open_orders: Iterable[Order],
) -> tuple[Decimal | None, Iterable[Position], Iterable[Order]]:
    if account_snapshot is None:
        return available_usdc, positions, open_orders
    if available_usdc is None:
        available_usdc = account_snapshot.available_usdc
    positions = account_snapshot.positions if not positions else positions
    open_orders = account_snapshot.open_orders if not open_orders else open_orders
    return available_usdc, positions, open_orders


def _unavailable_entry_plan(
    *,
    trace_id: str,
    market: Market | None,
    orderbook: OrderbookSnapshot | None,
    portfolio_budget_usdc: Decimal,
    reason: str,
    metadata: Mapping[str, Any] | None = None,
    sizing_extras: Mapping[str, Any] | None = None,
) -> EntryPlan:
    summary = _build_unavailable_summary(reason=reason, sizing_extras=sizing_extras)
    return EntryPlan(
        trace_id=trace_id,
        market=market,
        orderbook=orderbook,
        allocation_plan=AllocationPlan(
            trace_id=trace_id,
            total_budget_usdc=portfolio_budget_usdc,
            reason=reason,
        ),
        allocation=None,
        intent=None,
        eligible_market_count=0,
        reason=reason,
        summary=summary,
        metadata=metadata or {},
    )


def _build_unavailable_summary(
    *,
    reason: str,
    sizing_extras: Mapping[str, Any] | None = None,
) -> StrategySummary | None:
    """把 framework 已知的早期拒绝上下文投影成最小 StrategySummary。

    framework 不解释字段语义——sizing_extras 是策略 size_entry hook 返回的
    metadata，原样搬到 ``extras``，让 admin / virtual_paper 能展示 / 统计
    非-single_game 早期拒绝的诊断信息。
    """

    extras: dict[str, Any] = {}
    if sizing_extras:
        extras.update(dict(sizing_extras))
    if not extras and not reason:
        return None
    return StrategySummary(reason=reason, extras=extras)


def _open_orders_for(
    open_orders: tuple[Order, ...],
    condition_id: str,
    token_id: str,
) -> tuple[Order, ...]:
    return tuple(
        order
        for order in open_orders
        if order.condition_id == condition_id and order.token_id == token_id
    )


def _pick_allocation(
    allocations: tuple[Allocation, ...],
    condition_id: str,
    token_id: str,
) -> Allocation | None:
    for allocation in allocations:
        if allocation.condition_id == condition_id and allocation.token_id == token_id:
            return allocation
    return None


def _candidate_token_ids(market: Market) -> tuple[str, ...]:
    return market.token_ids


def _market_token_views(
    market: Market,
    *,
    orderbook_reader: OrderbookReader | None,
    account_snapshot: AccountSnapshot | None,
) -> tuple[MarketTokenView, ...]:
    return tuple(
        MarketTokenView(
            token_id=outcome.token_id,
            outcome=outcome.outcome,
            orderbook=(
                None
                if orderbook_reader is None
                else orderbook_reader(outcome.token_id)
            ),
            position=(
                None
                if account_snapshot is None
                else account_snapshot.get_position(market.condition_id, outcome.token_id)
            ),
            open_orders=(
                ()
                if account_snapshot is None
                else account_snapshot.open_orders_for_market(market.condition_id, outcome.token_id)
            ),
        )
        for outcome in market.outcomes
    )
