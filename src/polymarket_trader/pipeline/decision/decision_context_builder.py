from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from polymarket_trader.workflow.workflow import TradingWorkflow

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

from polymarket_trader.serialization import utc_now

from polymarket_trader.app.decision_recorder import (
    DecisionEventRecorder,
    build_decision_record_from_hook,
)
from polymarket_trader.app.intent_builder import (
    decision_to_managed_intent,
    decision_to_trade_intent,
)
from polymarket_trader.domain.trade_plan import TradePlan
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.allocation import AllocationPlan
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import ManagedOrderIntent, Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.decisions import DecisionContext, EntryCandidate, MarketTokenView, TradingDecision
from polymarket_trader.domain.decisions import ManualConfirmation
from polymarket_trader.domain.decisions import StrategySummary
from polymarket_trader.observability.trace import ensure_trace_id
from polymarket_trader.runtime.data_graph import DataGraph


OrderbookReader = Callable[[str], OrderbookSnapshot | None]


@dataclass(frozen=True, slots=True)
class _KellySizingState:
    """Kelly + bankroll 运行时参数包——替代 **kelly_kwargs 字典传递，让类型检查可以捕捉字段漂移。"""

    bankroll_usdc: Decimal
    kelly_fraction: Decimal
    kelly_max_position_fraction: Decimal
    kelly_min_edge: Decimal
    kelly_min_stake_usdc: Decimal
    kelly_allow_round_up_to_market_min: bool
    kelly_round_up_max_overbet_ratio: Decimal



class DecisionContextBuilder:
    """Bridge strategy decisions into framework plans and managed order intents."""

    def __init__(
        self,
        *,
        workflow: "TradingWorkflow",
        data_graph: DataGraph | None = None,
        decision_recorder: DecisionEventRecorder | None = None,
    ) -> None:
        """统一通过 DataGraph 读取 market / orderbook / position / open_orders。

        旧实现持有 `registry` + `orderbook_reader` callback 两个分散数据源——读
        同一 market 的不同字段要跨多个 store 调用。新版只持 DataGraph，调用方
        看到的是层次化 view（MarketView / OutcomeView），底层 4 store 聚合由
        DataGraph snapshot-and-release 完成（docs/新架构方案.md §3.1）。

        `data_graph=None` 是测试 / mock 友好的兼容口子——builder 内部 fallback
        return None；生产路径必传。
        """

        self._workflow = workflow
        self._graph = data_graph
        self._decision_recorder = decision_recorder

    def build_trade_plan(
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
        kelly_fraction: Decimal,
        kelly_max_position_fraction: Decimal,
        kelly_min_edge: Decimal,
        kelly_min_stake_usdc: Decimal,
        kelly_allow_round_up_to_market_min: bool = True,
        kelly_round_up_max_overbet_ratio: Decimal = Decimal("1"),
        positions: Iterable[Position] = (),
        open_orders: Iterable[Order] = (),
        metadata: Mapping[str, Any] | None = None,
        manual_confirmation: ManualConfirmation | None = None,
    ) -> TradePlan:
        trace_id = trace_id or ensure_trace_id()
        base_metadata = dict(metadata or {})
        available_usdc, positions, open_orders = _entry_account_inputs(
            account_snapshot=account_snapshot,
            available_usdc=available_usdc,
            positions=positions,
            open_orders=open_orders,
        )
        resolved_market = market or self.resolve_market(condition_id=condition_id, token_id=token_id)
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
            # §11 框架不自动 pause,但 admin MANUAL pause 仍是强门禁:
            # - 自动 pause(RECONCILE/RISK/sports_live_state_ended)已全删 → 不会出现在 _market_pauses
            # - admin pause_market_manual → 写入 _market_pauses(MANUAL source) → 这里 block
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

        bankroll_usdc = _resolve_bankroll(
            portfolio_budget_usdc=portfolio_budget_usdc,
            available_usdc=available_usdc,
        )
        kelly_state = _KellySizingState(
            bankroll_usdc=bankroll_usdc,
            kelly_fraction=kelly_fraction,
            kelly_max_position_fraction=kelly_max_position_fraction,
            kelly_min_edge=kelly_min_edge,
            kelly_min_stake_usdc=kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=kelly_allow_round_up_to_market_min,
            kelly_round_up_max_overbet_ratio=kelly_round_up_max_overbet_ratio,
        )
        # 单一决策调用——所有逻辑（candidate 过滤 / Kelly sizing / 价格判断 /
        # capital efficiency / position_plan metadata）全部由 QuantDecider class 内部完成。
        # 这层只负责 ① 拼上下文 ② 包装 QuantDecision 回 TradePlan 形状给 worker / admin。
        quant_context = self._quant_context(
            trace_id=trace_id,
            market=resolved_market,
            token_id=resolved_token_id,
            orderbook=resolved_orderbook,
            account_snapshot=account_snapshot,
            position=position_index.get((resolved_market.condition_id, focus_token_id)),
            open_orders=_open_orders_for(open_orders, resolved_market.condition_id, focus_token_id),
            entry_candidates=entry_candidates,
            portfolio_budget_usdc=portfolio_budget_usdc,
            available_usdc=available_usdc,
            kelly_state=kelly_state,
            metadata=base_metadata,
            manual_confirmation=manual_confirmation,
        )
        import time as _time
        _t0 = _time.perf_counter()
        quant_decision = self._workflow.quant_decide(quant_context)
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            SystemPerfMonitor.get().record_strategy_hook(
                "quant_decide", (_time.perf_counter() - _t0) * 1000
            )
        except Exception: pass
        signal_at = utc_now()
        from polymarket_trader.domain.decisions import TradeAction, TradingDecision
        decision = next(
            (a for a in quant_decision.actions if a.action == TradeAction.BUY),
            None,
        )
        if decision is None:
            decision = TradingDecision.skip(
                reason=quant_decision.reason or "quant_no_buy_action",
            )
        self._record(
            hook_name="quant_decide",
            context=quant_context,
            decision=decision,
        )
        reason = decision.reason or quant_decision.reason
        plan_metadata: dict[str, Any] = dict(base_metadata)
        plan_metadata.update(decision.metadata)
        decision_kind = decision.decision_kind
        summary = decision.summary
        intent = None
        if decision.action == TradeAction.BUY:
            merged_decision_metadata = dict(decision.metadata)
            merged_decision_metadata["signal_at"] = signal_at
            decision_with_signal = replace(decision, metadata=merged_decision_metadata)
            intent = decision_to_trade_intent(
                trace_id=trace_id,
                market=resolved_market,
                default_token_id=focus_token_id,
                decision=decision_with_signal,
            )
            if intent is None and decision.reason:
                reason = decision.reason
        if summary is None and reason:
            summary = StrategySummary(reason=reason)

        # allocation_plan 字段保留为占位（empty）——历史下游 (worker audit / risk
        # review) 仍读这字段；QuantDecider 单 market tick 视角下没有多候选分配的结构，
        # 保留空 AllocationPlan 让下游不崩，allocation 本身从 decision.metadata 拿。
        empty_plan = AllocationPlan(
            trace_id=trace_id,
            total_budget_usdc=portfolio_budget_usdc,
            reason=reason or "",
        )
        return TradePlan(
            trace_id=trace_id,
            market=resolved_market,
            orderbook=resolved_orderbook,
            allocation_plan=empty_plan,
            allocation=None,
            intent=intent,
            eligible_market_count=0,
            reason=reason,
            decision_kind=decision_kind,
            summary=summary,
            metadata=plan_metadata,
        )

    def _quant_context(
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
        kelly_state: _KellySizingState,
        metadata: Mapping[str, Any],
        manual_confirmation: ManualConfirmation | None = None,
    ) -> DecisionContext:
        """构造给 quant_decide hook 用的单 market_tick DecisionContext。

        历史 ``_sizing_context`` + ``_entry_decision_context`` 两份在 size_entry/
        decide_entry hook 分离时代各拼一遍；现在统一成一份，trigger_kind=market_tick。
        """
        effective_available_usdc = available_usdc if available_usdc is not None else portfolio_budget_usdc
        # 只把"非 DecisionContext 一等公民"的 budget 上下文塞进 metadata；Kelly 字段
        # 已在 DecisionContext 上有专用 attribute，不再镜像到 metadata 避免双口径漂移。
        context_metadata: dict[str, Any] = dict(metadata)
        context_metadata.update(
            {
                "portfolio_budget_usdc": portfolio_budget_usdc,
                "available_usdc": effective_available_usdc,
            }
        )
        return DecisionContext(
            trace_id=trace_id,
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
            bankroll_usdc=kelly_state.bankroll_usdc,
            kelly_fraction=kelly_state.kelly_fraction,
            kelly_max_position_fraction=kelly_state.kelly_max_position_fraction,
            kelly_min_edge=kelly_state.kelly_min_edge,
            kelly_min_stake_usdc=kelly_state.kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=kelly_state.kelly_allow_round_up_to_market_min,
            kelly_round_up_max_overbet_ratio=kelly_state.kelly_round_up_max_overbet_ratio,
            quant_trigger_kind="market_tick",
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
                    else self.lookup_orderbook(candidate_token_id)
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


    def quant_decide(self, context: DecisionContext):
        """量化决策器——所有 WS / 周期触发统一走这里。

        ``context.quant_trigger_kind`` 由调用方填写（"market_tick" / "reconcile_cycle"）。
        返回 QuantDecision；调用侧把 ``actions`` 转 intent 走统一
        OrderGateway/RiskManager。
        """
        import time as _time
        t0 = _time.perf_counter()
        decision = self._workflow.quant_decide(context)
        self._record_hook_latency("quant_decide", _time.perf_counter() - t0)
        self._record(hook_name="quant_decide", context=context, decision=decision)
        return decision

    @staticmethod
    def _record_hook_latency(name: str, elapsed_s: float) -> None:
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            SystemPerfMonitor.get().record_strategy_hook(name, elapsed_s * 1000)
        except Exception:
            pass

    def _record(
        self,
        *,
        hook_name: str,
        context: DecisionContext,
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
        if self._graph is None:
            return None
        if condition_id is not None:
            view = self._graph.market_view(condition_id)
            if view is not None:
                return view.market
        if token_id is not None:
            view = self._graph.market_view_for_token(token_id)
            if view is not None:
                return view.market
        return None

    def lookup_orderbook(self, token_id: str) -> OrderbookSnapshot | None:
        if self._graph is None:
            return None
        outcome = self._graph.outcome_view(token_id)
        return outcome.orderbook if outcome is not None else None

    def _resolve_orderbook(
        self,
        *,
        market: Market | None,
        token_id: str | None,
    ) -> OrderbookSnapshot | None:
        if token_id is None:
            return None
        return self.lookup_orderbook(token_id)

    def build_intent_from_decision(
        self,
        *,
        trace_id: str,
        condition_id: str,
        market_slug: str | None,
        default_token_id: str | None,
        decision: TradingDecision,
    ) -> ManagedOrderIntent | None:
        return decision_to_managed_intent(
            trace_id=trace_id,
            condition_id=condition_id,
            market_slug=market_slug,
            default_token_id=default_token_id,
            decision=decision,
        )


def _resolve_bankroll(
    *,
    portfolio_budget_usdc: Decimal,
    available_usdc: Decimal | None,
) -> Decimal:
    """Effective bankroll = ``min(链上 available, .env soft cap)``。

    ``available_usdc=None`` 直接返回 0——让 Kelly reject ``bankroll_non_positive``，
    避免"reconcile 没跑就用 .env 配置 sized 出超链上余额的 intent"。Paper / replay
    路径必须显式传 available_usdc。负值同样兜底到 0。
    """

    if available_usdc is None:
        return Decimal("0")
    bankroll = min(available_usdc, portfolio_budget_usdc)
    if bankroll < Decimal("0"):
        return Decimal("0")
    return bankroll


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
) -> TradePlan:
    summary = _build_unavailable_summary(reason=reason, sizing_extras=sizing_extras)
    return TradePlan(
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
