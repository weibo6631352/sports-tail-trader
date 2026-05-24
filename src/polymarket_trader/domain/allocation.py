from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from polymarket_trader.domain.order import Order, OrderSide, OrderType
from polymarket_trader.domain.position import Position


@dataclass(frozen=True, slots=True)
class Allocation:
    # strategy_id 必填，无默认值。框架/策略边界处必须显式提供；缺失直接抛错。
    strategy_id: str
    condition_id: str
    target_budget_usdc: Decimal
    buy_budget_usdc: Decimal
    market_slug: str | None = None
    token_id: str | None = None
    current_exposure_usdc: Decimal = Decimal("0")
    released_budget_usdc: Decimal = Decimal("0")
    reason: str = ""
    idempotency_key: str | None = None
    release_reason: str = ""
    # Kelly 决策审计字段（见 ``domain/kelly.py:KellyStake``）。
    # 全部 optional：旧 record-only / skip 路径不强制提供完整 Kelly 上下文，
    # 但任何被 EntryPlanner 接受的入场分配都应填齐 prob_p / price_c / f_star / edge。
    prob_p: Decimal | None = None
    prob_confidence: Decimal | None = None
    price_c: Decimal | None = None
    edge_net: Decimal | None = None
    edge_gross: Decimal | None = None
    fee_per_share_usdc: Decimal | None = None
    kelly_f_star: Decimal | None = None
    effective_kelly_fraction: Decimal | None = None
    effective_min_stake_usdc: Decimal | None = None
    capped_by: str | None = None
    is_round_up_overbet: bool = False


@dataclass(frozen=True, slots=True)
class MarketBuyBudgetChanged:
    condition_id: str
    market_slug: str | None
    token_id: str
    previous_buy_budget_usdc: Decimal
    new_buy_budget_usdc: Decimal
    released_budget_usdc: Decimal
    release_reason: str = ""
    current_exposure_usdc: Decimal = Decimal("0")
    target_budget_usdc: Decimal = Decimal("0")
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    trace_id: str
    total_budget_usdc: Decimal
    allocations: tuple[Allocation, ...] = field(default_factory=tuple)
    budget_changes: tuple[MarketBuyBudgetChanged, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def allocated_budget_usdc(self) -> Decimal:
        return sum((allocation.buy_budget_usdc for allocation in self.allocations), Decimal("0"))

    @property
    def released_budget_usdc(self) -> Decimal:
        return sum(
            (allocation.released_budget_usdc for allocation in self.allocations),
            Decimal("0"),
        )

    @property
    def unallocated_budget_usdc(self) -> Decimal:
        remaining = self.total_budget_usdc - self.allocated_budget_usdc
        return remaining if remaining > 0 else Decimal("0")

    @property
    def eligible_market_count(self) -> int:
        return sum(1 for allocation in self.allocations if allocation.target_budget_usdc > 0)


def current_exposure_usdc(position: Position | None, open_orders: Iterable[Order] = ()) -> Decimal:
    """计算 token 的 exposure(自身持仓 cost + 自身 open BUY orders).

    每个 (cid, token) 独立计算,不抵扣对面 token——双边持仓视为独立机会,
    每边按 Kelly 自己的 edge/edge 给完整预算.对冲收益由策略层独立决策.
    """
    exposure_usdc = Decimal("0")
    if position is not None and not position.settled_zero_value:
        exposure_usdc += position.cost_usdc

        # open SELL 代表已有底层持仓被挂单卖出，仍然占用原始持仓成本；按持仓成本比例近似计入 exposure。
        if position.shares > Decimal("0") and position.open_sell_shares > Decimal("0"):
            open_sell_shares = position.open_sell_shares
            if open_sell_shares > position.shares:
                open_sell_shares = position.shares
            exposure_usdc += position.cost_usdc * (open_sell_shares / position.shares)

    for order in open_orders:
        # 不变量：open SELL 在上面已经通过 position.open_sell_shares 按持仓成本
        # 比例计入 exposure。这里只处理 BUY 订单，避免与 open_sell_shares 重复
        # 计入。修改时务必保持「SELL 计入靠 position 字段，BUY 计入靠 open_orders」
        # 的二选一分工。
        if order.side != OrderSide.BUY:
            continue
        if order.order_type == OrderType.FAK:
            # FAK pending BUY 可能马上就会被吃掉或撤掉，热路径里按近似 0 处理，避免把短暂挂单放大成持仓。
            continue
        if order.amount_usdc is not None:
            exposure_usdc += order.amount_usdc
            continue
        if order.notional_usdc is not None:
            exposure_usdc += order.notional_usdc
            continue
        if order.price is not None and order.size_shares is not None:
            exposure_usdc += order.price * order.size_shares
    return exposure_usdc
