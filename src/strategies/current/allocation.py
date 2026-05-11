from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Iterable

from polymarket_trader.domain.allocation import (
    Allocation,
    AllocationPlan,
    MarketBuyBudgetChanged,
    current_exposure_usdc,
)
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order, OrderSide
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position

from strategies.current.identity import STRATEGY_ID

_MIN_CLOB_NOTIONAL_USDC = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class AllocationMarketSnapshot:
    market: Market
    token_id: str
    orderbook: OrderbookSnapshot | None = None
    position: Position | None = None
    open_orders: tuple[Order, ...] = field(default_factory=tuple)
    tradable: bool = True
    risk_allowed: bool = True
    market_active: bool = True
    market_open: bool = True
    clob_enabled: bool = True
    resolved: bool = False
    cancelled: bool = False
    archived: bool = False
    liquidity_usdc: Decimal | None = None
    spread: Decimal | None = None
    best_ask: Decimal | None = None
    best_ask_size: Decimal | None = None
    idempotency_key: str | None = None
    scale_in_allowed: bool = False
    strategy_budget_cap_usdc: Decimal | None = None

    @property
    def condition_id(self) -> str:
        return self.market.condition_id

    @property
    def market_slug(self) -> str:
        return self.market.market_slug


@dataclass(slots=True)
class _AllocationCandidate:
    snapshot: AllocationMarketSnapshot
    exposure_usdc: Decimal
    hard_capacity_usdc: Decimal
    liquidity_usdc: Decimal
    min_buy_budget_usdc: Decimal
    target_budget_usdc: Decimal = field(default_factory=lambda: Decimal("0"))
    buy_budget_usdc: Decimal = field(default_factory=lambda: Decimal("0"))
    release_reason: str = ""


def equal_weight_plan(
    *,
    trace_id: str,
    portfolio_budget_usdc: Decimal,
    markets: Iterable[AllocationMarketSnapshot],
    available_usdc: Decimal,
    max_order_usdc: Decimal,
    max_market_usdc: Decimal,
    max_total_usdc: Decimal,
) -> AllocationPlan:
    market_snapshots = tuple(markets)
    candidate_details: list[_AllocationCandidate] = []
    allocations: list[Allocation] = []
    budget_changes: list[MarketBuyBudgetChanged] = []
    plan_reason = ""

    total_exposure_usdc = Decimal("0")
    for snapshot in market_snapshots:
        exposure_usdc = current_exposure_usdc(snapshot.position, snapshot.open_orders)
        total_exposure_usdc += exposure_usdc
        skip_reason = _allocation_skip_reason(snapshot)
        liquidity_usdc = _market_liquidity_usdc(snapshot)
        hard_capacity_usdc = _market_hard_capacity_usdc(
            snapshot,
            exposure_usdc=exposure_usdc,
            available_usdc=available_usdc,
            max_order_usdc=max_order_usdc,
            max_market_usdc=max_market_usdc,
            liquidity_usdc=liquidity_usdc,
        )

        if skip_reason:
            allocations.append(
                Allocation(
                    strategy_id=STRATEGY_ID,
                    condition_id=snapshot.condition_id,
                    target_budget_usdc=Decimal("0"),
                    buy_budget_usdc=Decimal("0"),
                    market_slug=snapshot.market_slug,
                    token_id=snapshot.token_id,
                    current_exposure_usdc=exposure_usdc,
                    released_budget_usdc=Decimal("0"),
                    reason=skip_reason,
                    idempotency_key=snapshot.idempotency_key,
                    release_reason=skip_reason,
                )
            )
            continue

        candidate_details.append(
            _AllocationCandidate(
                snapshot=snapshot,
                exposure_usdc=exposure_usdc,
                hard_capacity_usdc=hard_capacity_usdc,
                liquidity_usdc=liquidity_usdc,
                min_buy_budget_usdc=_min_buy_budget_usdc(snapshot),
            )
        )

    eligible_count = len(candidate_details)
    if eligible_count == 0:
        plan_reason = "no_eligible_market"
        return AllocationPlan(
            trace_id=trace_id,
            total_budget_usdc=portfolio_budget_usdc,
            allocations=tuple(allocations),
            budget_changes=tuple(budget_changes),
            reason=plan_reason,
        )

    # 等权目标先按可交易、可风控、可吃到盘口深度的 market 数量平均，后续再把释放出来的额度重新分配。
    equal_weight_target_usdc = equal_weight_budget(portfolio_budget_usdc, eligible_count)
    remaining_pool_usdc = portfolio_budget_usdc
    if available_usdc < remaining_pool_usdc:
        remaining_pool_usdc = available_usdc
    remaining_total_capacity_usdc = max_total_usdc - total_exposure_usdc
    if remaining_total_capacity_usdc < remaining_pool_usdc:
        remaining_pool_usdc = remaining_total_capacity_usdc
    if remaining_pool_usdc < Decimal("0"):
        remaining_pool_usdc = Decimal("0")

    active_candidates = [
        candidate
        for candidate in candidate_details
        if candidate.hard_capacity_usdc >= candidate.min_buy_budget_usdc
    ]
    skipped_for_min_order = [candidate for candidate in candidate_details if candidate not in active_candidates]
    for candidate in skipped_for_min_order:
        candidate.release_reason = candidate.release_reason or "below_min_order_size"
    if not active_candidates:
        plan_reason = "no_market_meets_min_order_size"
    elif remaining_pool_usdc < min(
        (candidate.min_buy_budget_usdc for candidate in active_candidates),
        default=Decimal("0"),
    ):
        for candidate in active_candidates:
            candidate.release_reason = candidate.release_reason or "below_min_order_size"
        plan_reason = "total_budget_insufficient"
    else:
        while active_candidates and remaining_pool_usdc > Decimal("0"):
            per_market_target_usdc = equal_weight_budget(
                remaining_pool_usdc,
                len(active_candidates),
            )
            if per_market_target_usdc <= Decimal("0"):
                for candidate in active_candidates:
                    candidate.release_reason = candidate.release_reason or "below_min_order_size"
                plan_reason = "total_budget_insufficient"
                break

            next_active_candidates: list[_AllocationCandidate] = []
            allocated_this_round_usdc = Decimal("0")
            for candidate in active_candidates:
                min_buy_budget_usdc = candidate.min_buy_budget_usdc
                hard_capacity_usdc = candidate.hard_capacity_usdc
                previous_buy_budget_usdc = candidate.buy_budget_usdc
                available_capacity_usdc = hard_capacity_usdc - previous_buy_budget_usdc

                if available_capacity_usdc < min_buy_budget_usdc:
                    candidate.release_reason = candidate.release_reason or "market_limit_reached"
                    continue

                buy_budget_usdc = per_market_target_usdc
                release_reason = ""
                if buy_budget_usdc > available_capacity_usdc:
                    buy_budget_usdc = available_capacity_usdc
                    release_reason = _capacity_release_reason(
                        available_capacity_usdc=available_capacity_usdc,
                        hard_capacity_usdc=hard_capacity_usdc,
                        liquidity_usdc=candidate.liquidity_usdc,
                    )

                if buy_budget_usdc < min_buy_budget_usdc:
                    next_active_candidates.append(candidate)
                    continue

                candidate.buy_budget_usdc = previous_buy_budget_usdc + buy_budget_usdc
                candidate.target_budget_usdc = equal_weight_target_usdc
                allocated_this_round_usdc += buy_budget_usdc
                if buy_budget_usdc < per_market_target_usdc:
                    candidate.release_reason = candidate.release_reason or release_reason or "reallocated"

                remaining_capacity_after_buy_usdc = hard_capacity_usdc - candidate.buy_budget_usdc
                if remaining_capacity_after_buy_usdc >= min_buy_budget_usdc:
                    next_active_candidates.append(candidate)

            if allocated_this_round_usdc <= Decimal("0"):
                for candidate in active_candidates:
                    candidate.release_reason = candidate.release_reason or "below_min_order_size"
                plan_reason = "budget_remaining_below_min_order_size"
                break

            remaining_pool_usdc -= allocated_this_round_usdc
            if remaining_pool_usdc < Decimal("0"):
                remaining_pool_usdc = Decimal("0")
            active_candidates = next_active_candidates

            if active_candidates and equal_weight_budget(
                remaining_pool_usdc,
                len(active_candidates),
            ) < min(
                (candidate.min_buy_budget_usdc for candidate in active_candidates),
                default=Decimal("0"),
            ):
                plan_reason = "budget_remaining_below_min_order_size"
                break

    allocations.extend(
        _finalize_candidate_allocations(
            candidate_details,
            equal_weight_target_usdc=equal_weight_target_usdc,
        )
    )

    for candidate in candidate_details:
        snapshot = candidate.snapshot
        buy_budget_usdc = candidate.buy_budget_usdc
        target_budget_usdc = candidate.target_budget_usdc or equal_weight_target_usdc
        released_budget_usdc = target_budget_usdc - buy_budget_usdc
        if released_budget_usdc < Decimal("0"):
            released_budget_usdc = Decimal("0")

        release_reason = candidate.release_reason
        if buy_budget_usdc != target_budget_usdc or release_reason:
            budget_changes.append(
                MarketBuyBudgetChanged(
                    condition_id=snapshot.condition_id,
                    market_slug=snapshot.market_slug,
                    token_id=snapshot.token_id,
                    previous_buy_budget_usdc=target_budget_usdc,
                    new_buy_budget_usdc=buy_budget_usdc,
                    released_budget_usdc=released_budget_usdc,
                    release_reason=release_reason,
                    current_exposure_usdc=candidate.exposure_usdc,
                    target_budget_usdc=target_budget_usdc,
                    idempotency_key=snapshot.idempotency_key,
                )
            )

    return AllocationPlan(
        trace_id=trace_id,
        total_budget_usdc=portfolio_budget_usdc,
        allocations=tuple(allocations),
        budget_changes=tuple(budget_changes),
        reason=plan_reason,
    )


def equal_weight_budget(portfolio_budget_usdc: Decimal, eligible_market_count: int) -> Decimal:
    if eligible_market_count <= 0:
        return Decimal("0")
    return portfolio_budget_usdc / Decimal(eligible_market_count)


def _allocation_skip_reason(
    snapshot: AllocationMarketSnapshot,
) -> str:
    has_open_exit = _has_open_order(snapshot, OrderSide.SELL) or (
        snapshot.position is not None and snapshot.position.open_sell_shares > Decimal("0")
    )
    if has_open_exit and not snapshot.scale_in_allowed:
        return "open_exit_detected"
    if (
        snapshot.position is not None
        and snapshot.position.shares > Decimal("0")
        and not snapshot.scale_in_allowed
    ):
        return "position_already_open"
    if _has_open_order(snapshot, OrderSide.BUY):
        return "open_entry_detected"
    if not snapshot.tradable:
        return "market_not_tradable"
    if not snapshot.market_active:
        return "market_not_active"
    if not snapshot.market_open:
        return "market_not_open"
    if not snapshot.clob_enabled:
        return "clob_disabled"
    if snapshot.resolved:
        return "market_resolved"
    if snapshot.cancelled:
        return "market_cancelled"
    if snapshot.archived:
        return "market_archived"
    if not snapshot.risk_allowed:
        return "risk_limit_reached"

    return ""


def _has_open_order(snapshot: AllocationMarketSnapshot, side: OrderSide) -> bool:
    """判断当前 token 是否已有同方向开放订单，避免入场路径重复占仓。"""

    return any(order.side == side and order.open for order in snapshot.open_orders)


def _market_hard_capacity_usdc(
    snapshot: AllocationMarketSnapshot,
    *,
    exposure_usdc: Decimal,
    available_usdc: Decimal,
    max_order_usdc: Decimal,
    max_market_usdc: Decimal,
    liquidity_usdc: Decimal,
) -> Decimal:
    remaining_market_usdc = max_market_usdc - exposure_usdc
    if remaining_market_usdc < Decimal("0"):
        remaining_market_usdc = Decimal("0")

    hard_capacity_usdc = remaining_market_usdc
    if max_order_usdc < hard_capacity_usdc:
        hard_capacity_usdc = max_order_usdc
    fee_adjusted_available_usdc = _fee_adjusted_available_usdc(snapshot, available_usdc)
    if fee_adjusted_available_usdc < hard_capacity_usdc:
        hard_capacity_usdc = fee_adjusted_available_usdc
    if liquidity_usdc < hard_capacity_usdc:
        hard_capacity_usdc = liquidity_usdc
    if snapshot.strategy_budget_cap_usdc is not None and snapshot.strategy_budget_cap_usdc < hard_capacity_usdc:
        hard_capacity_usdc = snapshot.strategy_budget_cap_usdc
    if hard_capacity_usdc < Decimal("0"):
        return Decimal("0")
    return hard_capacity_usdc


def _market_liquidity_usdc(
    snapshot: AllocationMarketSnapshot,
) -> Decimal:
    if snapshot.liquidity_usdc is not None:
        return snapshot.liquidity_usdc
    return _ask_depth_notional(snapshot.orderbook)


def _fee_adjusted_available_usdc(snapshot: AllocationMarketSnapshot, available_usdc: Decimal) -> Decimal:
    """按 BUY taker 费用预留余额，避免把全部现金作为订单 amount 发出后被 CLOB 费用校验拒绝。"""

    if available_usdc <= Decimal("0") or snapshot.market.fees_enabled is False:
        return max(available_usdc, Decimal("0"))
    fee_rate_bps = snapshot.market.fee_rate_bps
    if fee_rate_bps is None:
        fee_rate_bps = snapshot.market.taker_base_fee_bps
    if fee_rate_bps is None or fee_rate_bps <= 0:
        return available_usdc
    price = snapshot.best_ask if snapshot.best_ask is not None else (
        snapshot.orderbook.best_ask if snapshot.orderbook is not None else None
    )
    if price is None or price <= Decimal("0") or price >= Decimal("1"):
        return available_usdc
    fee_multiplier = (Decimal(fee_rate_bps) / Decimal("1000")) * (Decimal("1") - price)
    if fee_multiplier <= Decimal("0"):
        return available_usdc
    adjusted = available_usdc / (Decimal("1") + fee_multiplier)
    return adjusted.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def _min_buy_budget_usdc(snapshot: AllocationMarketSnapshot) -> Decimal:
    """把 Polymarket 最小订单 size 换算为入场预算下限。

    `Market.min_order_size` 是份额下限；当前策略 BUY 意图传 USDC amount。
    因此预算分配阶段必须用计划入场价格估算 `size * price`，否则会把 5 shares
    误判成 5 USDC，导致尾盘低价机会被系统性跳过。
    """

    min_order_size = snapshot.market.min_order_size
    if min_order_size <= Decimal("0"):
        return Decimal("0")
    price = snapshot.best_ask if snapshot.best_ask is not None else (
        snapshot.orderbook.best_ask if snapshot.orderbook is not None else None
    )
    if price is None or price <= Decimal("0"):
        return max(min_order_size, _MIN_CLOB_NOTIONAL_USDC)
    return max(min_order_size * price, _MIN_CLOB_NOTIONAL_USDC)


def _ask_depth_notional(
    orderbook: OrderbookSnapshot | None,
) -> Decimal:
    if orderbook is None:
        return Decimal("0")
    depth_usdc = Decimal("0")
    levels = orderbook.asks
    if not levels and orderbook.best_ask is not None and orderbook.best_ask_size is not None:
        return orderbook.best_ask * orderbook.best_ask_size
    for level in levels:
        depth_usdc += level.price * level.size
    return depth_usdc


def _capacity_release_reason(
    *,
    available_capacity_usdc: Decimal,
    hard_capacity_usdc: Decimal,
    liquidity_usdc: Decimal,
) -> str:
    if available_capacity_usdc <= Decimal("0"):
        return "market_limit_reached"
    if liquidity_usdc < hard_capacity_usdc:
        return "depth_insufficient"
    return "single_market_limit_reached"


def _finalize_candidate_allocations(
    candidate_details: Iterable[_AllocationCandidate],
    *,
    equal_weight_target_usdc: Decimal,
) -> list[Allocation]:
    finalized: list[Allocation] = []
    for candidate in candidate_details:
        snapshot = candidate.snapshot
        buy_budget_usdc = candidate.buy_budget_usdc
        target_budget_usdc = candidate.target_budget_usdc or equal_weight_target_usdc
        released_budget_usdc = target_budget_usdc - buy_budget_usdc
        if released_budget_usdc < Decimal("0"):
            released_budget_usdc = Decimal("0")
        finalized.append(
            Allocation(
                strategy_id=STRATEGY_ID,
                condition_id=snapshot.condition_id,
                target_budget_usdc=target_budget_usdc,
                buy_budget_usdc=buy_budget_usdc,
                market_slug=snapshot.market_slug,
                token_id=snapshot.token_id,
                current_exposure_usdc=candidate.exposure_usdc,
                released_budget_usdc=released_budget_usdc,
                reason=candidate.release_reason,
                idempotency_key=snapshot.idempotency_key,
                release_reason=candidate.release_reason,
            )
        )
    return finalized
