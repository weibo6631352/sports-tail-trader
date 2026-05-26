"""体育扫尾的资金分配后置风险修正、成交统计与持仓覆盖度计算。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from polymarket_trader.domain.allocation import (
    Allocation,
    AllocationPlan,
    MarketBuyBudgetChanged,
)
from polymarket_trader.domain.order import OrderSide
from polymarket_trader.domain.decisions import DecisionContext

from polymarket_trader.workflow.allocation import AllocationMarketSnapshot
from polymarket_trader.workflow.config import TradingWorkflowConfig
from polymarket_trader.workflow.risk import check_tail_entry_risk
from polymarket_trader.workflow.trading.helpers import fill_notional_usdc


def _apply_tail_risk_limits(
    config: TradingWorkflowConfig,
    context: DecisionContext,
    *,
    plan: AllocationPlan,
    candidate_snapshots: tuple[AllocationMarketSnapshot, ...],
) -> tuple[AllocationPlan, dict[str, object]]:
    """按策略级风险上限修正 allocation plan。"""

    snapshot_by_key = {
        (snapshot.condition_id, snapshot.token_id): snapshot
        for snapshot in candidate_snapshots
    }
    focus_condition_id = context.market.condition_id if context.market is not None else None
    focus_token_id = context.token_id or (context.orderbook.token_id if context.orderbook is not None else None)
    updated_allocations: list[Allocation] = []
    extra_budget_changes: list[MarketBuyBudgetChanged] = []
    focus_metadata: dict[str, object] = {}
    for allocation in plan.allocations:
        key = (allocation.condition_id, allocation.token_id or "")
        snapshot = snapshot_by_key.get(key)
        if snapshot is None or allocation.buy_budget_usdc <= Decimal("0"):
            updated_allocations.append(allocation)
            continue
        risk_decision = check_tail_entry_risk(config, metadata=context.metadata)
        if allocation.condition_id == focus_condition_id and allocation.token_id == focus_token_id:
            focus_metadata.update(risk_decision.metadata or {})
        if risk_decision.passed:
            updated_allocations.append(allocation)
            continue
        updated = replace(
            allocation,
            buy_budget_usdc=Decimal("0"),
            released_budget_usdc=allocation.target_budget_usdc,
            reason=risk_decision.reason,
            release_reason=risk_decision.reason,
        )
        updated_allocations.append(updated)
        extra_budget_changes.append(
            MarketBuyBudgetChanged(
                condition_id=allocation.condition_id,
                market_slug=allocation.market_slug,
                token_id=allocation.token_id or "",
                previous_buy_budget_usdc=allocation.buy_budget_usdc,
                new_buy_budget_usdc=Decimal("0"),
                released_budget_usdc=allocation.buy_budget_usdc,
                release_reason=risk_decision.reason,
                current_exposure_usdc=allocation.current_exposure_usdc,
                target_budget_usdc=allocation.target_budget_usdc,
                idempotency_key=allocation.idempotency_key,
            )
        )
    if not extra_budget_changes:
        return plan, focus_metadata
    return (
        replace(
            plan,
            allocations=tuple(updated_allocations),
            budget_changes=(*plan.budget_changes, *extra_budget_changes),
            reason=plan.reason if plan.reason else "risk_limited",
        ),
        focus_metadata,
    )


def _covered_exit_shares(snapshot: AllocationMarketSnapshot) -> Decimal:
    """返回已有持仓被开放退出单覆盖的份额。"""

    position_covered = (
        Decimal("0")
        if snapshot.position is None
        else snapshot.position.open_sell_shares
    )
    order_covered = Decimal("0")
    for order in snapshot.open_orders:
        if order.side != OrderSide.SELL or not order.open:
            continue
        if order.remaining_shares is not None:
            order_covered += order.remaining_shares
        elif order.size_shares is not None:
            order_covered += order.size_shares
    return max(position_covered, order_covered)


def _buy_fill_summary(
    context: DecisionContext,
    snapshot: AllocationMarketSnapshot,
) -> tuple[int, Decimal]:
    """统计当前 token 的 BUY 成交次数和首笔入场金额，用于限制加仓。"""

    account = context.account_snapshot
    fills = account.fills if account is not None else ()
    buy_fills = [
        fill
        for fill in fills
        if fill.condition_id == snapshot.condition_id
        and fill.token_id == snapshot.token_id
        and (fill.side or "").upper() == "BUY"
    ]
    if not buy_fills:
        fallback_notional = Decimal("0") if snapshot.position is None else snapshot.position.cost_usdc
        return (1 if snapshot.position is not None else 0), fallback_notional
    first_notional = fill_notional_usdc(buy_fills[0])
    if first_notional <= Decimal("0") and snapshot.position is not None:
        first_notional = snapshot.position.cost_usdc
    return len(buy_fills), first_notional
