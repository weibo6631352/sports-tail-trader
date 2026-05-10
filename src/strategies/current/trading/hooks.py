"""当前策略的资金分配、入场和退出决策（三个公开 hook）。

不负责远端扫描，也不负责恢复修复语义。所有体育扫尾门禁、价格、profit-take
和风险修正逻辑都委派到子模块。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.extension_api import EntrySizing, ExtensionContext, ExtensionDecision

from strategies.current.allocation import AllocationMarketSnapshot, equal_weight_plan
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
    _sports_market_skip_metadata,
)
from .exit_overlay import _apply_profit_take_exit_plan, _sports_capital_efficiency_gate
from .gates import (
    _ask_depth_notional,
    _scale_in_allocation_gate,
    _scale_in_entry_gate,
    _sports_tail_allocation_gate,
    _sports_tail_entry_gate,
)
from .helpers import _metadata_decimal, _metadata_text
from .pricing import _sports_tail_locked_outcome_signal, _sports_tail_price_cap
from .risk_limits import _apply_sports_risk_limits


def size_entry(config: CurrentStrategyConfig, context: ExtensionContext) -> EntrySizing:
    """为当前 market 计算本轮可用入场预算。

    采用“等权分配”：先找出所有可参与分配的候选市场，再在这些市场之间平均分配预算。
    """

    portfolio_budget_usdc = context.portfolio_budget_usdc or _metadata_decimal(
        context,
        "portfolio_budget_usdc",
    )
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

    available_usdc = context.available_usdc or _metadata_decimal(context, "available_usdc")
    if available_usdc is None:
        available_usdc = portfolio_budget_usdc

    max_order_usdc = context.max_order_usdc or _metadata_decimal(context, "max_order_usdc")
    if max_order_usdc is None:
        return _empty_sizing(context, reason="missing_max_order_usdc")

    max_market_usdc = context.max_market_usdc or _metadata_decimal(context, "max_market_usdc")
    if max_market_usdc is None:
        return _empty_sizing(context, reason="missing_max_market_usdc")

    max_total_usdc = context.max_total_usdc or _metadata_decimal(context, "max_total_usdc")
    if max_total_usdc is None:
        return _empty_sizing(context, reason="missing_max_total_usdc")

    eligible_snapshots: list[AllocationMarketSnapshot] = []
    skipped_allocations: dict[tuple[str, str], Allocation] = {}
    sizing_metadata: dict[str, object] = {}
    for snapshot in candidate_snapshots:
        price_cap = _sports_tail_price_cap(
            config,
            snapshot.market,
            snapshot.token_id,
            locked_outcome_signal=_sports_tail_locked_outcome_signal(context),
        )
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
                sports_metadata = scale_in_metadata
            else:
                skip_reason, sports_metadata = _sports_tail_allocation_gate(
                    config,
                    context,
                    snapshot,
                    buyable_liquidity_usdc=buyable_liquidity_usdc,
                )
            if _is_focus_snapshot(context, snapshot):
                sizing_metadata.update(sports_metadata)
        if skip_reason:
            if _is_focus_snapshot(context, snapshot) and "sports_tail_reason" not in sizing_metadata:
                sizing_metadata.update(_sports_market_skip_metadata(snapshot, skip_reason))
            skipped_allocations[(snapshot.condition_id, snapshot.token_id)] = _skipped_allocation(
                snapshot,
                reason=skip_reason,
            )
            continue
        eligible_snapshots.append(replace(snapshot_for_allocation, liquidity_usdc=buyable_liquidity_usdc))

    eligible_plan = equal_weight_plan(
        trace_id=context.trace_id,
        portfolio_budget_usdc=portfolio_budget_usdc,
        markets=tuple(eligible_snapshots),
        available_usdc=available_usdc,
        max_order_usdc=max_order_usdc,
        max_market_usdc=max_market_usdc,
        max_total_usdc=max_total_usdc,
    )
    plan = _merge_allocation_plan(
        trace_id=context.trace_id,
        portfolio_budget_usdc=portfolio_budget_usdc,
        candidate_snapshots=candidate_snapshots,
        eligible_plan=eligible_plan,
        skipped_allocations=skipped_allocations,
    )
    plan, risk_metadata = _apply_sports_risk_limits(
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
    sports_gate = scale_in_gate or _sports_tail_entry_gate(config, context)
    if sports_gate is not None:
        decision, allowed_price, sports_metadata = sports_gate
        if decision is not None:
            return decision
    else:
        allowed_price = config.entry_no_price_max
        sports_metadata = {}

    best_ask = context.orderbook.best_ask
    if best_ask is None:
        return ExtensionDecision.skip(reason="missing_best_ask")
    if best_ask > allowed_price:
        return ExtensionDecision.skip(reason="price_above_entry_max")
    entry_price = best_ask

    amount_usdc = context.amount_usdc or _metadata_decimal(context, "amount_usdc", "buy_budget_usdc")
    if amount_usdc is None or amount_usdc <= Decimal("0"):
        return ExtensionDecision.skip(reason="missing_entry_amount")

    token_id = context.token_id or context.orderbook.token_id
    decision_metadata = dict(sports_metadata)
    efficiency_allowed, efficiency_reason, efficiency_metadata = _sports_capital_efficiency_gate(
        config,
        context,
        entry_price=entry_price,
        amount_usdc=amount_usdc,
        sports_metadata=decision_metadata,
    )
    decision_metadata.update(efficiency_metadata)
    if not efficiency_allowed:
        return ExtensionDecision.skip(reason=efficiency_reason, metadata=decision_metadata)
    decision_metadata.update(
        build_exit_plan_metadata(
            config,
            context,
            token_id=token_id,
            source_reason=str(sports_metadata.get("sports_tail_reason") or "strategy_entry"),
        )
    )
    _apply_profit_take_exit_plan(decision_metadata)
    decision_reason = "strategy_scale_in" if sports_metadata.get("sports_tail_opportunity_type") == (
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

    size_shares = context.size_shares or _metadata_decimal(context, "size_shares")
    if size_shares is not None and size_shares > Decimal("0"):
        uncovered_shares = size_shares
    elif context.position is not None:
        uncovered_shares = context.position.shares - context.position.open_sell_shares
    else:
        return ExtensionDecision.skip(reason="missing_position_state")
    if uncovered_shares <= Decimal("0"):
        return ExtensionDecision.skip(reason="no_uncovered_shares")

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
