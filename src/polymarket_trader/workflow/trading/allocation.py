"""把 DecisionContext 转成候选 AllocationMarketSnapshot，并计算 skip 原因。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.allocation import (
    Allocation,
    AllocationPlan,
    current_exposure_usdc,
)
from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.domain.decisions import DecisionContext, EntryCandidate

from polymarket_trader.workflow.allocation import AllocationMarketSnapshot
from polymarket_trader.workflow.config import TradingWorkflowConfig
from polymarket_trader.workflow.outcomes import describe_sports_market
from polymarket_trader.sports import LiveGameStatus, SportsMarketFamily
from polymarket_trader.sports.parsing import live_game_state_from_metadata


# 终态拦截集：进入这些状态的比赛不会再有确定性赢方（POSTPONED/CANCELLED/RETIRED
# 不会结算；DISPUTED 结算结果存疑；UNKNOWN/PAUSED 缺乏定价依据）——直接 skip 入场。
# **不包括 ENDED**：ENDED 是核心扫尾场景（赢方已确定但 Polymarket 未结算，挂赢方
# BUY 锁定确定性收益），由 math_prob 对输方返回 0 自然驱动 Kelly 拒败方。
# 参考：feedback_ended_market_prune_design.md
_TERMINAL_LIVE_STATUSES_BLOCKING_ENTRY = frozenset(
    {
        LiveGameStatus.POSTPONED,
        LiveGameStatus.CANCELLED,
        LiveGameStatus.RETIRED,
        LiveGameStatus.DISPUTED,
    }
)

from polymarket_trader.workflow.allocation import _ask_depth_notional


def _empty_sizing_plan(context: DecisionContext, reason: str) -> AllocationPlan:
    """构造一个“无可分配预算”的 AllocationPlan。"""

    from .helpers import _metadata_decimal
    total_budget_usdc = context.portfolio_budget_usdc or _metadata_decimal(
        context,
        "portfolio_budget_usdc",
    ) or Decimal("0")
    return AllocationPlan(
        trace_id=context.trace_id,
        total_budget_usdc=total_budget_usdc,
        reason=reason,
    )


def _candidate_snapshots(
    context: DecisionContext,
) -> tuple[AllocationMarketSnapshot, ...]:
    """从上下文中提取候选市场快照。

    正常路径下，框架会把候选市场列表放在 ``DecisionContext.entry_candidates``。
    如果当前调用点没有提供这个列表，这里会退化为只用当前 market 生成一个 fallback
    snapshot，保证逻辑仍可运行。
    """

    if context.entry_candidates:
        return tuple(_entry_candidate_to_snapshot(candidate) for candidate in context.entry_candidates)
    fallback = _fallback_snapshot(context)
    return () if fallback is None else (fallback,)


def _fallback_snapshot(
    context: DecisionContext,
) -> AllocationMarketSnapshot | None:
    """在缺少候选市场列表时，为当前 market 构造一个最小快照。"""

    if context.market is None or context.orderbook is None:
        return None
    return AllocationMarketSnapshot(
        market=context.market,
        token_id=context.token_id or context.orderbook.token_id,
        orderbook=context.orderbook,
        position=context.position,
        open_orders=context.open_orders,
        tradable=context.market.trading_status == TradingStatus.ELIGIBLE,
        risk_allowed=True,
        market_active=context.market.trading_status == TradingStatus.ELIGIBLE,
        market_open=context.market.trading_status == TradingStatus.ELIGIBLE,
        clob_enabled=True,
        resolved=context.market.trading_status == TradingStatus.RESOLVED,
        cancelled=False,
        archived=context.market.trading_status == TradingStatus.CLOSED,
        liquidity_usdc=_ask_depth_notional(context.orderbook),
        spread=context.orderbook.spread,
        best_ask=context.orderbook.best_ask,
        best_ask_size=context.orderbook.best_ask_size,
        idempotency_key=(
            f"{context.trace_id}:{context.market.condition_id}:{context.token_id or context.orderbook.token_id}"
        ),
    )


def _entry_candidate_to_snapshot(candidate: EntryCandidate) -> AllocationMarketSnapshot:
    return AllocationMarketSnapshot(
        market=candidate.market,
        token_id=candidate.token_id,
        orderbook=candidate.orderbook,
        position=candidate.position,
        open_orders=candidate.open_orders,
        tradable=candidate.market.trading_status == TradingStatus.ELIGIBLE,
        risk_allowed=True,
        market_active=candidate.market.trading_status == TradingStatus.ELIGIBLE,
        market_open=candidate.market.trading_status == TradingStatus.ELIGIBLE,
        clob_enabled=True,
        resolved=candidate.market.trading_status == TradingStatus.RESOLVED,
        cancelled=False,
        archived=candidate.market.trading_status == TradingStatus.CLOSED,
        liquidity_usdc=_ask_depth_notional(candidate.orderbook),
        spread=candidate.orderbook.spread,
        best_ask=candidate.orderbook.best_ask,
        best_ask_size=candidate.orderbook.best_ask_size,
        idempotency_key=candidate.idempotency_key,
    )


def _allocation_skip_reason(
    config: TradingWorkflowConfig,
    context: DecisionContext,
    snapshot: AllocationMarketSnapshot,
    *,
    buyable_liquidity_usdc: Decimal,
) -> str:
    # 已删 4 条上层门禁——回归 "trust Kelly + RiskManager"：
    # - open_entry_detected: OrderExecutor 自带 idempotency_key 防重提;Kelly bankroll
    #   扣减 + RiskManager bankroll_total_gate 防超额(§17 trust_kelly_no_extra_caps)
    # - open_exit_detected: 同 event 多盘口/多 outcome 是独立 edge,SELL 在飞不应
    #   阻挡其他 BUY(feedback_event_multi_condition_entry)
    # - position_already_open: 加仓由 Kelly + current_exposure_usdc 自然处理,
    #   focus 路径已被 _decide_market_tick 的 has_position 分支拦走
    # - unsupported_outcome: outside-model token 没真 prob_p → Kelly 自动以
    #   no_math_prob_signal 拒,不需要前置拦截
    # ws_eligible 已删——市场的 universe / 时间窗口判断由 discovery + market_ingest_service
    # 一次写入 registry，trading_status != ELIGIBLE 才是真正的"不可交易"信号。
    # 直播源状态审查：没直播源 / 直播源未适配 → 拒绝入场。
    # 决策必须基于活的直播状态（数学概率 / 末段守卫 / 概率视图都依赖 LiveGameState）；
    # 缺直播源等于盲下，缺解析适配等于"看得到数据但读不懂"，两种都视作高风险，宁可错过。
    descriptor = describe_sports_market(snapshot.market)
    if descriptor.market_family == SportsMarketFamily.UNSUPPORTED or descriptor.market_type is None:
        return "unsupported_market_family"
    if descriptor.market_family == SportsMarketFamily.SINGLE_GAME:
        game = live_game_state_from_metadata(context.metadata)
        if game is None:
            return "missing_live_game_state"
        # 终态比赛入场过滤：POSTPONED/CANCELLED/RETIRED/DISPUTED 没有明确赢方
        # 且不会正常结算，直接拒入场。**注意**：ENDED 不在此集合内——ENDED 是
        # 扫尾场景的核心（赢方 token BUY 锁定确定性收益），由 math_prob 对败方
        # 返回 0 让 Kelly 自然拒败方、放行赢方。
        if game.status in _TERMINAL_LIVE_STATUSES_BLOCKING_ENTRY:
            return f"live_game_terminal_{game.status.value}"
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

    best_ask = snapshot.effective_best_ask
    if best_ask is None:
        return "missing_best_ask"

    # 入场端只保留"物理/状态硬约束"——价格上限/下限、价差、流动性等门槛已删，
    # 由量化决策器统一管控。
    # 之前曾经存在的：
    #   - price_above_entry_max  → 删（持仓后 take_profit/stop_loss 接管）
    #   - spread_above_max       → 删（持仓后流动性问题由 exit overlay 处理）
    #   - liquidity_below_min    → 删（同上）
    return ""


def _market_skip_metadata(
    snapshot: AllocationMarketSnapshot,
    reason: str,
) -> dict[str, object]:
    """把 universe / allocation 早期跳过原因补成候选可读的体育审计字段。"""

    descriptor = describe_sports_market(snapshot.market)
    metadata: dict[str, object] = {
        "decision_action": "reject",
        "decision_reason": reason,
        "market_family": descriptor.market_family.value,
    }
    if descriptor.market_type is not None:
        metadata["market_type"] = descriptor.market_type.value
    for target in descriptor.targets:
        if target.token_id == snapshot.token_id:
            metadata["side"] = target.side.value
            break
    if descriptor.line is not None:
        metadata["line"] = str(descriptor.line)
    if snapshot.market.end_date is not None:
        metadata["market_end_date"] = snapshot.market.end_date.isoformat()
    if snapshot.best_ask is not None:
        metadata["best_ask"] = str(snapshot.best_ask)
    return metadata


def _skipped_allocation(
    snapshot: AllocationMarketSnapshot,
    *,
    reason: str,
) -> Allocation:
    return Allocation(
        condition_id=snapshot.condition_id,
        target_budget_usdc=Decimal("0"),
        buy_budget_usdc=Decimal("0"),
        market_slug=snapshot.market_slug,
        token_id=snapshot.token_id,
        current_exposure_usdc=current_exposure_usdc(snapshot.position, snapshot.open_orders),
        released_budget_usdc=Decimal("0"),
        reason=reason,
        idempotency_key=snapshot.idempotency_key,
        release_reason=reason,
    )


def _merge_allocation_plan(
    *,
    trace_id: str,
    portfolio_budget_usdc: Decimal,
    candidate_snapshots: tuple[AllocationMarketSnapshot, ...],
    eligible_plan: AllocationPlan,
    skipped_allocations: dict[tuple[str, str], Allocation],
) -> AllocationPlan:
    allocation_map = {
        (allocation.condition_id, allocation.token_id or ""): allocation
        for allocation in eligible_plan.allocations
    }
    allocation_map.update(skipped_allocations)
    ordered_allocations: list[Allocation] = []
    for snapshot in candidate_snapshots:
        allocation = allocation_map.get((snapshot.condition_id, snapshot.token_id))
        if allocation is not None:
            ordered_allocations.append(allocation)
    return AllocationPlan(
        trace_id=trace_id,
        total_budget_usdc=portfolio_budget_usdc,
        allocations=tuple(ordered_allocations),
        budget_changes=eligible_plan.budget_changes,
        reason=eligible_plan.reason,
    )


def _pick_allocation(
    allocations: tuple[Allocation, ...],
    condition_id: str | None,
    token_id: str | None,
) -> Allocation | None:
    """从分配结果中挑出当前目标 market 的那一项。"""

    if condition_id is None or token_id is None:
        return None
    for allocation in allocations:
        if allocation.condition_id == condition_id and allocation.token_id == token_id:
            return allocation
    return None


def _sizing_reason(plan: AllocationPlan, allocation: Allocation | None) -> str:
    """返回对外展示时更有解释力的 sizing 原因。

    优先使用当前 allocation 的具体原因；
    如果当前 market 没有单独原因，再退回整体 plan 的原因。
    """

    if allocation is not None and allocation.reason:
        return allocation.reason
    return plan.reason


def _is_focus_snapshot(
    context: DecisionContext,
    snapshot: AllocationMarketSnapshot,
) -> bool:
    """判断 allocation snapshot 是否对应当前触发入场判断的 token。"""

    if context.market is None:
        return False
    focus_token_id = context.token_id or (context.orderbook.token_id if context.orderbook is not None else None)
    return snapshot.condition_id == context.market.condition_id and snapshot.token_id == focus_token_id
