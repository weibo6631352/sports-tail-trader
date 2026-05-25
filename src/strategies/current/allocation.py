from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Iterable

from polymarket_trader.domain.allocation import (
    Allocation,
    AllocationPlan,
    MarketBuyBudgetChanged,
    current_exposure_usdc,
)
from polymarket_trader.domain.kelly import KellyStake, kelly_stake
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

    @property
    def effective_best_ask(self) -> Decimal | None:
        """best_ask 字段优先；缺时从 orderbook 取，避免调用方重复写三行兜底。"""
        if self.best_ask is not None:
            return self.best_ask
        if self.orderbook is not None:
            return self.orderbook.best_ask
        return None

    @property
    def effective_spread(self) -> Decimal | None:
        """spread 字段优先；缺时从 orderbook 取，避免调用方重复写三行兜底。"""
        if self.spread is not None:
            return self.spread
        if self.orderbook is not None:
            return self.orderbook.spread
        return None


# 策略侧给 Kelly 提供 (prob_p, prob_confidence, source_label) 的 callback。
# source_label 仅作审计标记（"outright_real" / "tail_implied" 等），不影响公式。
ProbProvider = Callable[[AllocationMarketSnapshot], "ProbView"]


@dataclass(frozen=True, slots=True)
class ProbView:
    prob_p: Decimal | None
    prob_confidence: Decimal
    source: str


def kelly_plan(
    *,
    trace_id: str,
    bankroll_usdc: Decimal,
    portfolio_budget_usdc: Decimal,
    markets: Iterable[AllocationMarketSnapshot],
    prob_provider: ProbProvider,
    kelly_fraction: Decimal,
    kelly_max_position_fraction: Decimal,
    kelly_min_edge: Decimal,
    kelly_min_stake_usdc: Decimal,
    kelly_allow_round_up_to_market_min: bool = True,
    kelly_round_up_max_overbet_ratio: Decimal = Decimal("1"),
) -> AllocationPlan:
    """Kelly 资金分配。替代旧 equal_weight_plan。

    工作流：
    1. 对每个 market 跑 ``_allocation_skip_reason``——保留旧 eligibility 语义。
    2. 对 eligible market 用 prob_provider 取 ``(prob_p, prob_confidence)``，
       与 ``best_ask`` 一起喂 ``kelly_stake()``。
    3. 按 ``f_star`` 降序排序——edge 大的优先得到 bankroll。
    4. **§1 sequential bankroll**：依次分配，``remaining_bankroll = bankroll -
       Σ(已开 BUY exposure) - Σ(本轮已分配 stake)``。避免并发 over-bet。
    5. 每个 Allocation 带完整 Kelly 审计字段（prob_p / edge / f_star / capped_by 等）。

    skip 与 reject 一律 buy_budget=0 + reason 写明，不静默丢弃。
    """

    market_snapshots = tuple(markets)
    allocations: list[Allocation] = []
    budget_changes: list[MarketBuyBudgetChanged] = []
    plan_reason = ""

    # 单次扫描：一遍出 (skip_reason / exposure / prob_view / price_c / kelly_estimate)
    # 全部信息；避免之前"skip filter loop + estimate loop + alloc loop"三次重算
    # current_exposure 与 _compute_kelly。kelly_estimate 用初始 remaining_bankroll
    # 仅作排序键——真分配阶段再用滚动 remaining_bankroll 重算 stake。
    @dataclass(slots=True)
    class _Candidate:
        snapshot: AllocationMarketSnapshot
        exposure_usdc: Decimal
        prob_view: ProbView
        price_c: Decimal
        estimate_f_star: Decimal

    eligible: list[_Candidate] = []
    open_exposure_total = Decimal("0")
    for snapshot in market_snapshots:
        exposure_usdc = current_exposure_usdc(snapshot.position, snapshot.open_orders)
        open_exposure_total += exposure_usdc
        skip_reason = _allocation_skip_reason(snapshot)
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
        prob_view = prob_provider(snapshot)
        price_c = snapshot.effective_best_ask
        if prob_view.prob_p is None or price_c is None or price_c <= Decimal("0"):
            allocations.append(
                _reject_allocation(
                    snapshot,
                    exposure_usdc=exposure_usdc,
                    reason="missing_price_or_prob",
                    prob_view=prob_view,
                    price_c=price_c,
                )
            )
            continue
        # f_star 与 bankroll 无关，只看 (p, c)；用任意非零 bankroll 跑一次拿 f_star 排序键。
        # bankroll=1 让 ``raw_stake`` 微小不影响排序，但 f_star 准确反映 edge/denom。
        estimate = _compute_kelly(
            snapshot=snapshot,
            bankroll_usdc=Decimal("1"),
            prob_view=prob_view,
            price_c=price_c,
            kelly_fraction=kelly_fraction,
            kelly_max_position_fraction=kelly_max_position_fraction,
            kelly_min_edge=kelly_min_edge,
            kelly_min_stake_usdc=Decimal("0"),
            kelly_allow_round_up_to_market_min=False,
            kelly_round_up_max_overbet_ratio=Decimal("1"),
        )
        eligible.append(_Candidate(snapshot, exposure_usdc, prob_view, price_c, estimate.f_star))

    if not eligible:
        plan_reason = "no_eligible_market"
        return AllocationPlan(
            trace_id=trace_id,
            total_budget_usdc=portfolio_budget_usdc,
            allocations=tuple(allocations),
            budget_changes=tuple(budget_changes),
            reason=plan_reason,
        )

    remaining_bankroll = bankroll_usdc - open_exposure_total
    if remaining_bankroll < Decimal("0"):
        remaining_bankroll = Decimal("0")

    eligible.sort(key=lambda item: item.estimate_f_star, reverse=True)

    # mutually_exclusive_loser gate 已删——宽进严管：即便同 condition 多 outcome
    # 各自有 edge 信号也允许同时下单，由持仓策略 + Kelly 自身的 fraction 管控
    # 总暴露。3-way prop / NEG_RISK 同 market 多 token 都按独立 candidate 评估。

    for candidate in eligible:
        snapshot = candidate.snapshot
        prob_view = candidate.prob_view
        price_c = candidate.price_c
        exposure_usdc = candidate.exposure_usdc
        stake = _compute_kelly(
            snapshot=snapshot,
            bankroll_usdc=remaining_bankroll,
            prob_view=prob_view,
            price_c=price_c,
            kelly_fraction=kelly_fraction,
            kelly_max_position_fraction=kelly_max_position_fraction,
            kelly_min_edge=kelly_min_edge,
            kelly_min_stake_usdc=kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=kelly_allow_round_up_to_market_min,
            kelly_round_up_max_overbet_ratio=kelly_round_up_max_overbet_ratio,
        )
        if stake.stake_usdc <= Decimal("0"):
            allocations.append(
                _reject_allocation(
                    snapshot,
                    exposure_usdc=exposure_usdc,
                    reason=stake.reject_reason or "kelly_zero",
                    prob_view=prob_view,
                    price_c=price_c,
                    kelly=stake,
                )
            )
            continue
        # 应用策略侧 strategy_budget_cap_usdc（scale-in 路径设的额外硬上限）
        stake_usdc = stake.stake_usdc
        capped_by = stake.capped_by
        if (
            snapshot.strategy_budget_cap_usdc is not None
            and stake_usdc > snapshot.strategy_budget_cap_usdc
        ):
            stake_usdc = snapshot.strategy_budget_cap_usdc
            capped_by = "strategy_budget_cap"
        allocations.append(
            Allocation(
                strategy_id=STRATEGY_ID,
                condition_id=snapshot.condition_id,
                target_budget_usdc=stake_usdc,
                buy_budget_usdc=stake_usdc,
                market_slug=snapshot.market_slug,
                token_id=snapshot.token_id,
                current_exposure_usdc=exposure_usdc,
                released_budget_usdc=Decimal("0"),
                reason="kelly_sized",
                idempotency_key=snapshot.idempotency_key,
                release_reason="",
                prob_p=prob_view.prob_p,
                prob_confidence=prob_view.prob_confidence,
                price_c=price_c,
                edge_net=stake.edge,
                edge_gross=stake.edge_gross,
                fee_per_share_usdc=stake.fee_per_share_usdc,
                kelly_f_star=stake.f_star,
                effective_kelly_fraction=stake.effective_kelly_fraction,
                effective_min_stake_usdc=stake.effective_min_stake_usdc,
                capped_by=capped_by,
                is_round_up_overbet=stake.is_round_up_overbet,
            )
        )
        remaining_bankroll -= stake_usdc
        if remaining_bankroll < Decimal("0"):
            remaining_bankroll = Decimal("0")

    if not any(allocation.buy_budget_usdc > Decimal("0") for allocation in allocations):
        plan_reason = plan_reason or "no_kelly_stake"

    return AllocationPlan(
        trace_id=trace_id,
        total_budget_usdc=portfolio_budget_usdc,
        allocations=tuple(allocations),
        budget_changes=tuple(budget_changes),
        reason=plan_reason,
    )


def _compute_kelly(
    *,
    snapshot: AllocationMarketSnapshot,
    bankroll_usdc: Decimal,
    prob_view: ProbView,
    price_c: Decimal,
    kelly_fraction: Decimal,
    kelly_max_position_fraction: Decimal,
    kelly_min_edge: Decimal,
    kelly_min_stake_usdc: Decimal,
    kelly_allow_round_up_to_market_min: bool,
    kelly_round_up_max_overbet_ratio: Decimal,
) -> KellyStake:
    fee_rate_bps = snapshot.market.fee_rate_bps
    if fee_rate_bps is None:
        fee_rate_bps = snapshot.market.taker_base_fee_bps or 0
    return kelly_stake(
        bankroll_usdc=bankroll_usdc,
        price_c=price_c,
        fair_value_p=prob_view.prob_p or Decimal("0"),
        side="BUY_YES",
        kelly_fraction=kelly_fraction,
        prob_confidence=prob_view.prob_confidence,
        max_position_fraction=kelly_max_position_fraction,
        min_edge=kelly_min_edge,
        min_stake_usdc=kelly_min_stake_usdc,
        market_min_order_size_shares=snapshot.market.min_order_size,
        fee_rate_bps=fee_rate_bps,
        fees_enabled=snapshot.market.fees_enabled is not False,
        liquidity_usdc=_market_liquidity_usdc(snapshot),
        allow_round_up_to_market_min=kelly_allow_round_up_to_market_min,
        round_up_max_overbet_ratio=kelly_round_up_max_overbet_ratio,
    )


def _reject_allocation(
    snapshot: AllocationMarketSnapshot,
    *,
    exposure_usdc: Decimal,
    reason: str,
    prob_view: ProbView | None = None,
    price_c: Decimal | None = None,
    kelly: KellyStake | None = None,
) -> Allocation:
    return Allocation(
        strategy_id=STRATEGY_ID,
        condition_id=snapshot.condition_id,
        target_budget_usdc=Decimal("0"),
        buy_budget_usdc=Decimal("0"),
        market_slug=snapshot.market_slug,
        token_id=snapshot.token_id,
        current_exposure_usdc=exposure_usdc,
        released_budget_usdc=Decimal("0"),
        reason=reason,
        idempotency_key=snapshot.idempotency_key,
        release_reason=reason,
        prob_p=prob_view.prob_p if prob_view is not None else None,
        prob_confidence=prob_view.prob_confidence if prob_view is not None else None,
        price_c=price_c,
        edge_net=kelly.edge if kelly is not None else None,
        edge_gross=kelly.edge_gross if kelly is not None else None,
        fee_per_share_usdc=kelly.fee_per_share_usdc if kelly is not None else None,
        kelly_f_star=kelly.f_star if kelly is not None else None,
        effective_kelly_fraction=kelly.effective_kelly_fraction if kelly is not None else None,
        effective_min_stake_usdc=kelly.effective_min_stake_usdc if kelly is not None else None,
        capped_by=kelly.capped_by if kelly is not None else None,
        is_round_up_overbet=kelly.is_round_up_overbet if kelly is not None else False,
    )


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


def _market_liquidity_usdc(
    snapshot: AllocationMarketSnapshot,
) -> Decimal:
    if snapshot.liquidity_usdc is not None:
        return snapshot.liquidity_usdc
    return _ask_depth_notional(snapshot.orderbook)


def _ask_depth_notional(
    orderbook: OrderbookSnapshot | None,
) -> Decimal:
    # 无价格上限版本，仅供 kelly_plan 内部使用；有 price_cap 的版本在 trading/gates.py。
    if orderbook is None:
        return Decimal("0")
    depth_usdc = Decimal("0")
    levels = orderbook.asks
    if not levels and orderbook.best_ask is not None and orderbook.best_ask_size is not None:
        return orderbook.best_ask * orderbook.best_ask_size
    for level in levels:
        depth_usdc += level.price * level.size
    return depth_usdc
