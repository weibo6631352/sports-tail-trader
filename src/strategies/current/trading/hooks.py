"""当前策略的资金分配、入场和退出决策（三个公开 hook）。

不负责远端扫描，也不负责恢复修复语义。所有体育扫尾门禁、价格、profit-take
和风险修正逻辑都委派到子模块。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.kelly import implied_fair_value_from_price_cap
from polymarket_trader.extension_api import EntrySizing, ExtensionContext, ExtensionDecision

from strategies.current.allocation import (
    AllocationMarketSnapshot,
    ProbView,
    kelly_plan,
)
from strategies.current.config import CurrentStrategyConfig
from strategies.current.exit_plan import build_exit_plan_metadata, exit_price_for_context

from .allocation import (
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
from .exit_overlay import _apply_profit_take_exit_plan, _capital_efficiency_gate
from .gates import (
    _ask_depth_notional,
    _scale_in_allocation_gate,
    _scale_in_entry_gate,
    _tail_allocation_gate,
    _tail_entry_gate,
)
from .helpers import _metadata_text
from .pricing import _tail_locked_outcome_signal, _tail_price_cap
from .risk_limits import _apply_tail_risk_limits


def size_entry(config: CurrentStrategyConfig, context: ExtensionContext) -> EntrySizing:
    """为当前 market 计算本轮可用入场预算（Kelly sizing）。

    流程：
    1. 走原有 tail / scale-in 门禁过滤候选；通过的进入 eligible_snapshots。
    2. ``prob_provider`` 把 tail price_cap + min_edge 反推 implied_fair_value，
       作为 Kelly 公式吃的 ``prob_p``；prob_confidence=tail_implied_prob_confidence
       （默认 0.5）抑制 implied 的不确定性。
    3. ``kelly_plan`` 按 f_star 降序逐笔分配，bankroll 扣减保证不并发 over-bet。
    4. ``_apply_tail_risk_limits`` 套相关性硬上限（事件 / 联赛 / 日新增）。
    """

    # EntryPlanner 是 ExtensionContext 的唯一构造方，所有 kelly_* / bankroll
    # 字段都在 ``_sizing_context`` 里强制写入。缺失只能是契约违反，直接抛错
    # 让 supervisor 抓到，比静默返回 missing_xxx 更早暴露问题。
    portfolio_budget_usdc = context.portfolio_budget_usdc
    if portfolio_budget_usdc is None:
        return _empty_sizing(context, reason="missing_portfolio_budget")

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
            "ExtensionContext 缺少 Kelly 配置字段——EntryPlanner 应当强制写入"
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
    snapshot_price_cap: dict[tuple[str, str], Decimal] = {}
    for snapshot in candidate_snapshots:
        price_cap = _tail_price_cap(
            config,
            snapshot.market,
            snapshot.token_id,
            locked_outcome_signal=_tail_locked_outcome_signal(context),
        )
        snapshot_price_cap[(snapshot.condition_id, snapshot.token_id)] = price_cap
        buyable_liquidity_usdc = _ask_depth_notional(
            snapshot.orderbook,
            price_cap=price_cap,
        )
        scale_in_allowed, scale_in_metadata, scale_in_budget_cap = _scale_in_allocation_gate(
            config,
            context,
            snapshot,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
        )
        snapshot_for_allocation = replace(
            snapshot,
            scale_in_allowed=scale_in_allowed,
            strategy_budget_cap_usdc=scale_in_budget_cap,
        )
        skip_reason = _allocation_skip_reason(
            config,
            context,
            snapshot_for_allocation,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
        )
        if not skip_reason:
            if scale_in_allowed:
                tail_metadata = scale_in_metadata
            else:
                skip_reason, tail_metadata = _tail_allocation_gate(
                    config,
                    context,
                    snapshot,
                    buyable_liquidity_usdc=buyable_liquidity_usdc,
                )
            if _is_focus_snapshot(context, snapshot):
                sizing_metadata.update(tail_metadata)
        if skip_reason:
            if _is_focus_snapshot(context, snapshot) and "tail_reason" not in sizing_metadata:
                sizing_metadata.update(_market_skip_metadata(snapshot, skip_reason))
            skipped_allocations[(snapshot.condition_id, snapshot.token_id)] = _skipped_allocation(
                snapshot,
                reason=skip_reason,
            )
            continue
        eligible_snapshots.append(replace(snapshot_for_allocation, liquidity_usdc=buyable_liquidity_usdc))

    implied_min_edge_required = Decimal(config.tail_implied_min_edge_bps) / Decimal("10000")
    implied_prob_confidence_base = config.tail_implied_prob_confidence
    depth_baseline = config.tail_implied_conf_depth_baseline_usdc
    spread_widening = config.tail_implied_conf_spread_widening

    def _prob_provider(snap: AllocationMarketSnapshot) -> ProbView:
        cap = snapshot_price_cap.get((snap.condition_id, snap.token_id))
        if cap is None:
            cap = _tail_price_cap(
                config,
                snap.market,
                snap.token_id,
                locked_outcome_signal=_tail_locked_outcome_signal(context),
            )
        implied_p = implied_fair_value_from_price_cap(cap, min_edge_required=implied_min_edge_required)
        # 动态 conf：流动性薄 / 价差宽时 implied_p 更不可靠 → κ 进一步收缩。
        # 公式 = base × min(1, depth/baseline) × max(0.25, 1 - spread/widening)
        # baseline=25, widening=0.05 → conf 范围 [base/8, base]。
        depth_factor = Decimal("1")
        if snap.liquidity_usdc is not None and depth_baseline > Decimal("0"):
            ratio = snap.liquidity_usdc / depth_baseline
            if ratio < Decimal("1"):
                depth_factor = ratio if ratio > Decimal("0") else Decimal("0")
        spread_factor = Decimal("1")
        if snap.spread is not None and spread_widening > Decimal("0"):
            shrink = snap.spread / spread_widening
            spread_factor = max(Decimal("0.25"), Decimal("1") - shrink) if shrink < Decimal("1") else Decimal("0.25")
        confidence = implied_prob_confidence_base * depth_factor * spread_factor
        if confidence < Decimal("0"):
            confidence = Decimal("0")
        return ProbView(
            prob_p=implied_p,
            prob_confidence=confidence,
            source="tail_implied",
        )

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
    plan, risk_metadata = _apply_tail_risk_limits(
        config,
        context,
        plan=plan,
        candidate_snapshots=candidate_snapshots,
    )
    sizing_metadata.update(risk_metadata)
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


def decide_entry(config: CurrentStrategyConfig, context: ExtensionContext) -> ExtensionDecision:
    """根据盘口和预算生成 BUY 决策。"""

    if context.market is None or context.orderbook is None:
        return ExtensionDecision.skip(reason="missing_market_state")

    scale_in_gate = _scale_in_entry_gate(config, context)
    tail_gate = scale_in_gate or _tail_entry_gate(config, context)
    if tail_gate is not None:
        decision, allowed_price, tail_metadata = tail_gate
        if decision is not None:
            return decision
    else:
        from strategies.current.parameter_overrides import effective_decimal

        allowed_price = effective_decimal(None, "entry_no_price_max", config.entry_no_price_max)
        tail_metadata = {}

    best_ask = context.orderbook.best_ask
    if best_ask is None:
        return ExtensionDecision.skip(reason="missing_best_ask")
    if best_ask > allowed_price:
        return ExtensionDecision.skip(reason="price_above_entry_max")
    entry_price = best_ask

    amount_usdc = context.amount_usdc
    if amount_usdc is None or amount_usdc <= Decimal("0"):
        return ExtensionDecision.skip(reason="missing_entry_amount")

    token_id = context.token_id or context.orderbook.token_id
    decision_metadata = dict(tail_metadata)
    efficiency_allowed, efficiency_reason, efficiency_metadata = _capital_efficiency_gate(
        config,
        context,
        entry_price=entry_price,
        amount_usdc=amount_usdc,
        tail_metadata=decision_metadata,
    )
    decision_metadata.update(efficiency_metadata)
    if not efficiency_allowed:
        return ExtensionDecision.skip(reason=efficiency_reason, metadata=decision_metadata)
    decision_metadata.update(
        build_exit_plan_metadata(
            config,
            context,
            token_id=token_id,
            source_reason=str(tail_metadata.get("tail_reason") or "strategy_entry"),
        )
    )
    _apply_profit_take_exit_plan(decision_metadata)
    decision_reason = "strategy_scale_in" if tail_metadata.get("opportunity_type") == (
        "scale_in_advantage"
    ) else "strategy_entry"
    return ExtensionDecision.buy(
        reason=decision_reason,
        token_id=token_id,
        price=entry_price,
        amount_usdc=amount_usdc,
        order_type=None,
        post_only=False,
        market_slug=context.market.market_slug,
        metadata=decision_metadata,
    )


def decide_exit(config: CurrentStrategyConfig, context: ExtensionContext) -> ExtensionDecision:
    """根据持仓状态生成 SELL 决策。"""

    if not config.auto_exit_enabled:
        return ExtensionDecision.skip(reason="settlement_only_exit_disabled")

    size_shares = context.size_shares
    if size_shares is not None and size_shares > Decimal("0"):
        uncovered_shares = size_shares
    elif context.position is not None:
        uncovered_shares = context.position.shares - context.position.open_sell_shares
    else:
        return ExtensionDecision.skip(reason="missing_position_state")
    if uncovered_shares <= Decimal("0"):
        return ExtensionDecision.skip(reason="no_uncovered_shares")

    # 跳过已结算/关闭市场中的僵尸仓位：当前值为 0 且盘口不存在，
    # 说明市场已结束且无流动性，此时挂 SELL 只会被立即拒绝。
    if (
        context.position is not None
        and (context.position.current_value is None or context.position.current_value <= Decimal("0"))
        and context.orderbook is None
    ):
        return ExtensionDecision.skip(reason="position_zero_value_no_orderbook")

    token_id = (
        context.token_id
        or (context.position.token_id if context.position is not None else None)
        or _metadata_text(context, "token_id")
    )
    decision_metadata = build_exit_plan_metadata(
        config,
        context,
        token_id=token_id,
        source_reason="strategy_exit",
        target_size_shares=uncovered_shares,
    )
    return ExtensionDecision.sell(
        reason="strategy_exit",
        token_id=token_id,
        price=exit_price_for_context(config, context),
        size_shares=uncovered_shares,
        market_slug=(
            context.market.market_slug if context.market is not None else _metadata_text(context, "market_slug")
        ),
        metadata=decision_metadata,
    )


def _empty_sizing(context: ExtensionContext, *, reason: str) -> EntrySizing:
    """构造一个“无可分配预算”的占位结果。"""

    return EntrySizing(
        allocation_plan=_empty_sizing_plan(context, reason),
        reason=reason,
    )
