"""量化决策器——所有 BUY / SELL / HOLD / replace 决策的单一入口。

按 ``context.quant_trigger_kind`` 分派子流程：
- ``market_tick``：market_ws book / price_change 触发
- ``reconcile_cycle``：周期扫账户 → 清理僵尸订单 / pause 信号

类内不持有可变运行时状态（每次 ``decide`` 调用从 context 拿最新快照）。
入场走 Kelly + quant_signal 真概率信号；持仓时本模块不预设任何动作——
后续买卖决策由用户在量化信号入口（fair_value / Kelly 或自有信号源）接入。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.decisions import DecisionContext, EntrySizing, QuantDecision, TradingDecision
from polymarket_trader.runtime.runtime_ports import RuntimePorts

from polymarket_trader.workflow.allocation import (
    AllocationMarketSnapshot,
    ProbView,
    kelly_plan,
)
from polymarket_trader.app.signal_snapshot_store import SignalSnapshotStore
from polymarket_trader.workflow.config import TradingWorkflowConfig
from polymarket_trader.workflow.quant_signal import estimate_signal
from polymarket_trader.workflow.trading.allocation import (
    _allocation_skip_reason,
    _candidate_snapshots,
    _empty_sizing_plan,
    _is_focus_snapshot,
    _merge_allocation_plan,
    _pick_allocation,
    _sizing_reason,
    _skipped_allocation,
    _market_skip_metadata,
)
from polymarket_trader.workflow.allocation import _ask_depth_notional

logger = logging.getLogger(__name__)


def size_entry(
    config: TradingWorkflowConfig,
    context: DecisionContext,
    signal_snapshot_store: SignalSnapshotStore | None = None,
) -> EntrySizing:
    """为当前 market 计算本轮可用入场预算（Kelly sizing）。

    流程：
    1. 走门禁过滤候选；通过的进入 eligible_snapshots。
    2. ``prob_provider`` 仅消费 quant_signal.math_prob 真概率信号——
       没真信号 → ProbView(prob_p=None) → kelly_plan 直接 reject。Kelly 不会被
       反推/虚假 prob 喂养（trust Kelly fully）。
    3. ``kelly_plan`` 按 f_star 降序逐笔分配，bankroll 扣减保证不并发 over-bet。
       Kelly 自带单笔 fraction 上限和 drawdown halt，**不在 Kelly 之上叠加任何 cap**
       （CLAUDE.md §17 / feedback_trust_kelly_no_extra_caps）。
    """

    # EntryPlanner 是 DecisionContext 的唯一构造方，所有 kelly_* / bankroll
    # 字段都在 ``_sizing_context`` 里强制写入。缺失只能是契约违反，直接抛错
    # 让 supervisor 抓到，比静默返回 missing_xxx 更早暴露问题。
    portfolio_budget_usdc = context.portfolio_budget_usdc
    if portfolio_budget_usdc is None:
        return _empty_sizing(context, reason="missing_portfolio_budget")

    # 可用现金硬下限：低于 $5 不入场。读 context.available_usdc 内存值，不调 API。
    # 现金过低时即便 Kelly 算出 stake 也凑不出最小订单 + 留不出 fee buffer，
    # 直接早退避免后续候选枚举/评估浪费。
    if context.available_usdc is not None and context.available_usdc < Decimal("5"):
        return _empty_sizing(context, reason="insufficient_available_cash")

    candidate_snapshots = _candidate_snapshots(context)
    if not candidate_snapshots:
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=portfolio_budget_usdc,
                reason="missing_market_state",
            ),
            reason="missing_market_state",
        )

    bankroll_usdc = context.bankroll_usdc
    if bankroll_usdc is None:
        bankroll_usdc = portfolio_budget_usdc
    # R15 (架构师 Round 7) bankroll staleness 归因：决策时刻 last_reconcile_at 与 now
    # 差距秒数。喂 Kelly 的 bankroll 若超过 market_sync_interval_seconds (20s) + buffer
    # 就是 stale，决策质量打折扣。account_snapshot 缺时（启动早 / fixture）= None。
    account_age_s: Decimal | None = None
    if context.account_snapshot is not None:
        last_reconcile = context.account_snapshot.last_reconcile_at
        if last_reconcile is not None:
            from datetime import datetime as _dt, timezone as _tz
            account_age_s = Decimal(str((_dt.now(_tz.utc) - last_reconcile).total_seconds()))
    kelly_fraction = context.kelly_fraction
    kelly_max_position_fraction = context.kelly_max_position_fraction
    kelly_min_edge = context.kelly_min_edge if context.kelly_min_edge is not None else Decimal("0")
    kelly_min_stake_usdc = context.kelly_min_stake_usdc
    if (
        kelly_fraction is None
        or kelly_max_position_fraction is None
        or kelly_min_stake_usdc is None
    ):
        raise ValueError(
            "DecisionContext 缺少 Kelly 配置字段——EntryPlanner 应当强制写入"
        )
    kelly_allow_round_up = (
        context.kelly_allow_round_up_to_market_min
        if context.kelly_allow_round_up_to_market_min is not None
        else True
    )
    kelly_round_up_max_overbet_ratio = context.kelly_round_up_max_overbet_ratio or Decimal("1")

    eligible_snapshots: list[AllocationMarketSnapshot] = []
    skipped_allocations: dict[tuple[str, str], Allocation] = {}
    sizing_metadata: dict[str, object] = {}
    for snapshot in candidate_snapshots:
        buyable_liquidity_usdc = _ask_depth_notional(snapshot.orderbook)
        skip_reason = _allocation_skip_reason(
            config,
            context,
            snapshot,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
        )
        if skip_reason:
            if _is_focus_snapshot(context, snapshot) and "decision_reason" not in sizing_metadata:
                sizing_metadata.update(_market_skip_metadata(snapshot, skip_reason))
            skipped_allocations[(snapshot.condition_id, snapshot.token_id)] = _skipped_allocation(
                snapshot,
                reason=skip_reason,
            )
            continue
        eligible_snapshots.append(replace(snapshot, liquidity_usdc=buyable_liquidity_usdc))

    def _prob_provider(snap: AllocationMarketSnapshot) -> ProbView:
        """Kelly probability source —— 走 estimate_signal 单一入口 (R7 CPO Round 3)。

        estimate_signal 内部做真概率信号 max 融合（goalserve devig + math_prob）+
        盘口兜底（microprice / mid / best_bid）四层链 + 同步产 LiveSignalSnapshot
        写入 store 供 /analytics/edge-signals 暴露。

        **策略安全红线**：只有真概率层 (goalserve_implied_prob / math_prob) 才返
        prob_p ≠ None；盘口兜底层 prob_p=None → Kelly 拒绝（不能用 microprice 当
        真信号触发下单——会被 best_bid 污染形成自反馈）。
        """

        snap_ob = snap.orderbook
        if snap_ob is None or snap_ob.best_bid is None:
            return ProbView(prob_p=None, prob_confidence=Decimal("0"), source="missing_orderbook")
        focus_context = (
            context if context.token_id == snap.token_id else replace(context, token_id=snap.token_id)
        )
        prob, source, snapshot = estimate_signal(
            focus_context,
            token_id=snap.token_id,
            best_bid=snap_ob.best_bid,
            best_ask=snap_ob.best_ask,
            account_age_s=account_age_s,
        )
        if signal_snapshot_store is not None:
            signal_snapshot_store.record(snapshot)
        # 只有真概率层喂 Kelly；盘口兜底层 → prob_p=None 让 Kelly 拒。
        if source in {"goalserve_implied_prob", "math_prob"}:
            return ProbView(prob_p=prob, prob_confidence=Decimal("0.7"), source=source)
        return ProbView(prob_p=None, prob_confidence=Decimal("0"), source=source)

    eligible_plan = kelly_plan(
        trace_id=context.trace_id,
        bankroll_usdc=bankroll_usdc,
        portfolio_budget_usdc=portfolio_budget_usdc,
        markets=tuple(eligible_snapshots),
        prob_provider=_prob_provider,
        kelly_fraction=kelly_fraction,
        kelly_max_position_fraction=kelly_max_position_fraction,
        kelly_min_edge=kelly_min_edge,
        kelly_min_stake_usdc=kelly_min_stake_usdc,
        kelly_allow_round_up_to_market_min=kelly_allow_round_up,
        kelly_round_up_max_overbet_ratio=kelly_round_up_max_overbet_ratio,
    )
    plan = _merge_allocation_plan(
        trace_id=context.trace_id,
        portfolio_budget_usdc=portfolio_budget_usdc,
        candidate_snapshots=candidate_snapshots,
        eligible_plan=eligible_plan,
        skipped_allocations=skipped_allocations,
    )
    allocation = _pick_allocation(
        plan.allocations,
        context.market.condition_id if context.market is not None else None,
        context.token_id or (context.orderbook.token_id if context.orderbook is not None else None),
    )
    return EntrySizing(
        allocation_plan=plan,
        allocation=allocation,
        reason=_sizing_reason(plan, allocation),
        metadata=sizing_metadata,
    )


def decide_entry(config: TradingWorkflowConfig, context: DecisionContext) -> TradingDecision:
    """根据盘口和预算生成 BUY 决策。

    门禁已在 size_entry / _allocation_skip_reason 通过——这里只负责落 BUY intent
    并附带 position_plan（进场后止盈/止损/scale-in 等行为）。
    """

    if context.market is None or context.orderbook is None:
        return TradingDecision.skip(reason="missing_market_state")

    best_ask = context.orderbook.best_ask
    if best_ask is None:
        return TradingDecision.skip(reason="missing_best_ask")
    entry_price = best_ask

    amount_usdc = context.amount_usdc
    if amount_usdc is None or amount_usdc <= Decimal("0"):
        return TradingDecision.skip(reason="missing_entry_amount")

    token_id = context.token_id or context.orderbook.token_id
    decision_metadata: dict[str, object] = {}
    # Kelly 内核审计字段——decide_entry 时 context.allocation 已含 Kelly 计算结果,
    # 平铺到 decision.metadata 让 decision_records 可见 prob_p / fair_value / edge /
    # f_star / capped_by 等核心数据(§15 量化复盘 + 策略调参依赖).
    allocation = context.allocation
    if allocation is not None:
        decision_metadata["kelly"] = {
            "prob_p": str(allocation.prob_p) if allocation.prob_p is not None else None,
            "prob_confidence": (
                str(allocation.prob_confidence) if allocation.prob_confidence is not None else None
            ),
            "price_c": str(allocation.price_c) if allocation.price_c is not None else None,
            "edge_gross": str(allocation.edge_gross) if allocation.edge_gross is not None else None,
            "edge_net": str(allocation.edge_net) if allocation.edge_net is not None else None,
            "fee_per_share_usdc": (
                str(allocation.fee_per_share_usdc) if allocation.fee_per_share_usdc is not None else None
            ),
            "f_star": str(allocation.kelly_f_star) if allocation.kelly_f_star is not None else None,
            "effective_kelly_fraction": (
                str(allocation.effective_kelly_fraction)
                if allocation.effective_kelly_fraction is not None
                else None
            ),
            "effective_min_stake_usdc": (
                str(allocation.effective_min_stake_usdc)
                if allocation.effective_min_stake_usdc is not None
                else None
            ),
            "capped_by": allocation.capped_by,
            "is_round_up_overbet": allocation.is_round_up_overbet,
            "buy_budget_usdc": str(allocation.buy_budget_usdc),
            "target_budget_usdc": str(allocation.target_budget_usdc),
            "current_exposure_usdc": str(allocation.current_exposure_usdc),
        }
    from polymarket_trader.domain.decisions import DecisionSummary
    from polymarket_trader.workflow.outcomes import describe_sports_market
    descriptor = describe_sports_market(context.market)
    summary = DecisionSummary(
        action="auto_execute",
        reason="quant_entry",
        market_type=descriptor.market_type.value if descriptor.market_type is not None else "",
        best_ask=entry_price,
        extras={
            "market_family": descriptor.market_family.value,
            "execution_permission": "auto_execute",
        },
    )
    return TradingDecision.buy(
        reason="quant_entry",
        token_id=token_id,
        price=entry_price,
        amount_usdc=amount_usdc,
        order_type=None,
        post_only=False,
        market_slug=context.market.market_slug,
        metadata=decision_metadata,
        summary=summary,
    )


def _position_entry_price(context: DecisionContext) -> Decimal | None:
    """估算我方持仓的实际买入均价（cost / shares）。

    动态止盈/止损都以买入均价为基准；无持仓或数据异常（零份额/零成本）时
    返回 None，调用侧退回静态退出价。
    """

    position = context.position
    if position is None or position.shares <= Decimal("0") or position.cost_usdc <= Decimal("0"):
        return None
    return position.cost_usdc / position.shares


def _empty_sizing(context: DecisionContext, *, reason: str) -> EntrySizing:
    """构造一个“无可分配预算”的占位结果。"""

    return EntrySizing(
        allocation_plan=_empty_sizing_plan(context, reason),
        reason=reason,
    )


class QuantDecider:
    """量化决策器。

    所有 WS 盘口 tick / reconcile 周期触发的"持仓后该怎么动"决策都从这里出。
    类内不持有可变运行时状态（每次 ``decide`` 调用从 context 拿最新快照），
    保留 ``ports`` 引用便于未来接入 parameter store / metrics。
    """

    def __init__(
        self,
        *,
        config: TradingWorkflowConfig,
        ports: RuntimePorts | None = None,
        signal_snapshot_store: SignalSnapshotStore | None = None,
    ) -> None:
        self._config = config
        self._ports = ports
        # R7 CPO 指令：store 注入是 caller 的责任，QuantDecider 持有 + 传给 size_entry
        # → _prob_provider 写入。None = 测试 / fixture 路径不强制写入。
        self._signal_snapshot_store = signal_snapshot_store

    # ---- 主入口 -----------------------------------------------------

    def decide(self, context: DecisionContext) -> QuantDecision:
        trigger = context.quant_trigger_kind
        if trigger == "market_tick":
            return self._decide_market_tick(context)
        if trigger == "reconcile_cycle":
            return self._decide_reconcile(context)
        return QuantDecision(actions=(), reason=f"unknown_trigger_kind:{trigger}")

    # ---- market_tick：盘口事件路径 ----------------------------------

    def _decide_market_tick(self, context: DecisionContext) -> QuantDecision:
        """市场盘口事件触发：单一入口决策当前动作。

        分派规则：
        - 有持仓 → ``_decide_position_action``（SELL / replace）
        - 无持仓 → ``_decide_entry_attempt``（BUY）
        """
        has_position = (
            context.position is not None and context.position.shares > Decimal("0")
        )
        if has_position:
            decision = self._decide_position_action(context)
        else:
            decision = self._decide_entry_attempt(context)
        if decision.action.value == "skip":
            return QuantDecision(actions=(), reason=decision.reason)
        return QuantDecision(actions=(decision,), reason=decision.reason)

    def _decide_entry_attempt(self, context: DecisionContext) -> TradingDecision:
        """无持仓时的入场决策——所有 family 走同一份主路径。

        - 所有 SportsMarketFamily 统一调 ``size_entry`` + ``decide_entry``；
        - 单一信号入口是 ``estimate_signal``（math_prob / goalserve / microprice 三层），
          没真信号的市场 → ProbView(prob_p=None) → Kelly 拒绝。

        ESPORTS 之类没量化锁定信号的 family 由 universe 层(workflow/universe.py)
        在 discovery 阶段拒绝;这里不再二次拦截——belt-and-suspenders 已移除。
        """
        if context.market is None or context.orderbook is None:
            return TradingDecision.skip(reason="missing_market_state")

        sizing = size_entry(self._config, context, self._signal_snapshot_store)
        if sizing.allocation is None or sizing.allocation.buy_budget_usdc <= Decimal("0"):
            return TradingDecision.skip(reason=sizing.reason or "no_allocation")
        focus_context = replace(
            context,
            amount_usdc=sizing.allocation.buy_budget_usdc,
            allocation=sizing.allocation,
            allocation_plan=sizing.allocation_plan,
        )
        return decide_entry(self._config, focus_context)

    def _decide_position_action(self, context: DecisionContext) -> TradingDecision:
        """持仓时 market_tick——量化决策器**不预设任何动作**。

        所有持仓后的买卖决策（SELL / replace / HOLD）由用户在主量化决策入口
        （quant_signal / Kelly / 自有信号源）后续接入产生；本方法当前 skip。

        注：曾接入三档退场（CPO R5 越界夹带）已按 CPO R2 决议撤回，等专项
        协评 + backtest 后再单独提议（参 task list R5 夹带处置表）。
        """

        return TradingDecision.skip(reason="quant_position_hold")

    # ---- reconcile_cycle：周期路径 ----------------------------------

    def _decide_reconcile(self, context: DecisionContext) -> QuantDecision:
        from polymarket_trader.workflow.recovery import build_recovery_quant_decision
        return build_recovery_quant_decision(self._config, context)


