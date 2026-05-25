"""量化决策器类——所有持仓期决策的单一入口。

按 ``context.quant_trigger_kind`` 分派子流程：
- ``market_tick``：market_ws book / price_change 触发的盘口事件 → BUY / SELL / replace
- ``reconcile_cycle``：周期扫账户 → 清理僵尸订单 / pause 信号

类内不持有可变运行时状态（每次 ``decide`` 调用从 context 拿最新快照）。
原 ``polymarket_trader.quant.trading.hooks`` 的 size_entry / decide_entry /
_maybe_reprice_stale_sell / _math_lock_prob_view / _position_entry_price /
_empty_sizing 全部并入本模块，作为 QuantDecider 的内部实现。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.kelly import implied_fair_value_from_price_cap
from polymarket_trader.domain.order import OrderSide
from polymarket_trader.extension_api import (
    EntrySizing,
    ExtensionContext,
    ExtensionDecision,
    ExtensionPorts,
    QuantDecision,
)

from polymarket_trader.quant.allocation import (
    AllocationMarketSnapshot,
    ProbView,
    kelly_plan,
)
from polymarket_trader.quant.config import CurrentStrategyConfig
from polymarket_trader.quant.position_plan import build_position_plan_metadata
from polymarket_trader.quant.trading.allocation import (
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
from polymarket_trader.quant.trading.exit_overlay import (
    _apply_profit_take_position_plan,
    _capital_efficiency_gate,
    evaluate_dynamic_exit,
)
from polymarket_trader.quant.trading.gates import _ask_depth_notional
from polymarket_trader.quant.trading.helpers import _metadata_text
from polymarket_trader.quant.trading.risk_limits import _apply_tail_risk_limits

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _maybe_reprice_stale_sell(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    now: datetime,
) -> ExtensionDecision | None:
    """检查 token 下现有 SELL 单是否价位 stale，需 cancel-replace 到 entry+offset。

    两条触发路径（任一命中即 replace）：
    1. **profit_take 路径**：SELL price > entry+offset + 2 tick → 替换到 entry+offset
       （§17 准量化提前止盈，旧 $0.99 死等结算最大单一损失源）
    2. **fair_value 路径**（原有）：SELL price > fair_value × 1.5 → 替换到
       max(best_bid+tick, fair_value × 0.95)（防 fair value 大幅跌穿后死单）

    替换价不超过原 SELL price - tick，避免 cancel-replace 价更高反而难成交。
    """

    from polymarket_trader.quant.trading.exit_overlay import _estimate_fair_value, _exit_orderbook

    token_id = (
        context.token_id
        or (context.position.token_id if context.position is not None else None)
    )
    if token_id is None:
        logger.info("reprice_skip", extra={"reason": "no_token_id"})
        return None
    open_sells = [
        o for o in (context.open_orders or ())
        if o.side == OrderSide.SELL and o.token_id == token_id and o.open and o.remaining_shares
    ]
    if not open_sells:
        logger.info(
            "reprice_skip",
            extra={
                "reason": "no_open_sells",
                "token_id": token_id,
                "open_orders_count": len(context.open_orders or ()),
                "open_orders_sides": [o.side.value for o in (context.open_orders or ())],
            },
        )
        return None
    orderbook = _exit_orderbook(context, token_id)
    if orderbook is None or orderbook.best_bid is None:
        logger.info(
            "reprice_skip",
            extra={
                "reason": "no_orderbook" if orderbook is None else "no_best_bid",
                "token_id": token_id,
                "open_sells_count": len(open_sells),
                "open_sells_prices": [str(s.price) for s in open_sells],
            },
        )
        return None
    fair_value, fair_source = _estimate_fair_value(
        context,
        token_id=token_id,
        best_bid=orderbook.best_bid,
        best_ask=orderbook.best_ask,
    )
    tick = orderbook.tick_size or Decimal("0.01")
    # 目标 SELL 价 = max(entry+min_offset, fair_value × 0.97)
    # × 0.97 留 3% 缓冲让对手方愿意吃单（按 CLOB 撮合规则按对手价成交，留缓冲
    # 实际可能成交在更高价）。min_offset 保证至少覆盖手续费（30bps × 2 = 0.6%）。
    min_profit_offset = Decimal("0.02")
    target_from_fair = (fair_value * Decimal("0.97")).quantize(Decimal("0.001"))
    target_from_entry: Decimal | None = None
    position = context.position
    if (
        position is not None
        and position.shares > Decimal("0")
        and position.cost_usdc > Decimal("0")
    ):
        avg_price = position.cost_usdc / position.shares
        target_from_entry = avg_price + min_profit_offset
    if target_from_entry is not None:
        sell_target = max(target_from_fair, target_from_entry)
    else:
        sell_target = target_from_fair
    # cap：不超过 exit_no_price（避免 CLOB 上限拒单）
    sell_target = min(sell_target, config.exit_no_price - tick)

    for sell in open_sells:
        sell_price = sell.price
        if sell_price is None:
            continue
        # 触发：当前 SELL 价比目标价高 ≥ 2 tick 且远离实际可成交盘口
        # 同时保护：如果 SELL 已接近 fair_value × 1.05，认为已经合理不动
        if sell_price <= sell_target + tick * Decimal("2"):
            logger.info(
                "reprice_skip",
                extra={
                    "reason": "sell_price_already_close_to_target",
                    "token_id": token_id,
                    "sell_price": str(sell_price),
                    "sell_target": str(sell_target),
                    "fair_value": str(fair_value),
                    "fair_value_source": fair_source,
                },
            )
            continue
        # 新价：tick 对齐到 sell_target，但不超过原价 - tick（避免反向更难成交）
        # 必须最后再做一次 floor 对齐——上游 sell_price 可能来自历史未对齐挂单
        # （如旧版本入场逻辑或手工挂单），sell_price - tick 不保证是 tick 倍数。
        # 不对齐直接进 RiskManager 会被 tick_size_invalid 拒，反复重试刷日志。
        aligned_target = (
            (sell_target / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        )
        raw_new_price = min(aligned_target, sell_price - tick)
        new_price = (raw_new_price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        if new_price <= Decimal("0") or new_price >= sell_price:
            continue
        return ExtensionDecision.replace(
            order_id=sell.order_id,
            token_id=token_id,
            price=new_price,
            size_shares=sell.remaining_shares or sell.size_shares or Decimal("0"),
            market_slug=context.market.market_slug if context.market else None,
            reason="exit_overlay_reprice_to_fair_value",
            metadata={
                "old_price": str(sell_price),
                "new_price": str(new_price),
                "fair_value": str(fair_value),
                "fair_value_source": fair_source,
                "sell_target": str(sell_target),
                "target_from_fair": str(target_from_fair),
                "target_from_entry": (
                    str(target_from_entry) if target_from_entry is not None else None
                ),
                "best_bid": str(orderbook.best_bid),
            },
        )
    return None


def _math_lock_prob_view(
    snap: AllocationMarketSnapshot,
    context: ExtensionContext,
) -> "ProbView | None":
    """math_lock fallback: 没 odds 源时用统一数学模型估真概率作 Kelly p。

    返回 ProbView with prob_p = lock_probability,confidence=0.7(数学模型置信度
    比真实赔率低,补偿模型简化导致的估计误差)。method=unsupported / lock_p=0 时
    返回 None,让 caller fallback 到下一层(implied 反推等)。
    """

    from polymarket_trader.sports.math_lock import evaluate_math_lock
    from polymarket_trader.sports.parsing import live_game_state_from_metadata
    from polymarket_trader.quant.outcomes import describe_sports_market, target_for_token

    market = snap.market
    descriptor = describe_sports_market(market)
    if not descriptor.accepted or descriptor.market_type is None:
        return None
    target = target_for_token(market, snap.token_id)
    if target is None:
        return None
    game = live_game_state_from_metadata(context.metadata)
    if game is None:
        return None
    lock_result = evaluate_math_lock(
        descriptor.market_type,
        target.side,
        descriptor.line,
        game,
        market_slug=market.market_slug,
    )
    if lock_result.method == "unsupported" or lock_result.lock_probability <= Decimal("0"):
        return None
    # math_lock 输出 [0,1] 锁定概率 = 我方持仓胜率近似。用作 Kelly prob_p。
    # confidence=0.7: 数学模型基于 base rate 估计,不如真实 odds 准,留缓冲。
    return ProbView(
        prob_p=lock_result.lock_probability,
        prob_confidence=Decimal("0.7"),
        source=f"math_lock:{lock_result.method}",
    )


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
    for snapshot in candidate_snapshots:
        buyable_liquidity_usdc = _ask_depth_notional(snapshot.orderbook)
        skip_reason = _allocation_skip_reason(
            config,
            context,
            snapshot,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
        )
        if skip_reason:
            if _is_focus_snapshot(context, snapshot) and "tail_reason" not in sizing_metadata:
                sizing_metadata.update(_market_skip_metadata(snapshot, skip_reason))
            skipped_allocations[(snapshot.condition_id, snapshot.token_id)] = _skipped_allocation(
                snapshot,
                reason=skip_reason,
            )
            continue
        eligible_snapshots.append(replace(snapshot, liquidity_usdc=buyable_liquidity_usdc))

    implied_min_edge_required = Decimal(config.tail_implied_min_edge_bps) / Decimal("10000")
    implied_prob_confidence_base = config.tail_implied_prob_confidence
    depth_baseline = config.tail_implied_conf_depth_baseline_usdc
    spread_widening = config.tail_implied_conf_spread_widening

    def _prob_provider(snap: AllocationMarketSnapshot) -> ProbView:
        """Kelly probability source — math_lock 优先，fallback implied_fair_value。"""

        math_view = _math_lock_prob_view(snap, context)
        if math_view is not None:
            return math_view

        cap = config.tail_locked_outcome_max_entry_price
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
    """根据盘口和预算生成 BUY 决策。

    门禁已在 size_entry / _allocation_skip_reason 通过——这里只负责落 BUY intent
    并附带 position_plan（进场后止盈/止损/scale-in 等行为）。
    """

    if context.market is None or context.orderbook is None:
        return ExtensionDecision.skip(reason="missing_market_state")

    best_ask = context.orderbook.best_ask
    if best_ask is None:
        return ExtensionDecision.skip(reason="missing_best_ask")
    entry_price = best_ask

    amount_usdc = context.amount_usdc
    if amount_usdc is None or amount_usdc <= Decimal("0"):
        return ExtensionDecision.skip(reason="missing_entry_amount")

    token_id = context.token_id or context.orderbook.token_id
    decision_metadata: dict[str, object] = {}
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
        build_position_plan_metadata(
            config,
            context,
            token_id=token_id,
            source_reason="strategy_entry",
            entry_price=entry_price,
        )
    )
    _apply_profit_take_position_plan(decision_metadata)
    from polymarket_trader.extension_api.summary import StrategySummary
    from polymarket_trader.quant.outcomes import describe_sports_market
    descriptor = describe_sports_market(context.market)
    summary = StrategySummary(
        action="auto_execute",
        reason="strategy_entry",
        market_type=descriptor.market_type.value if descriptor.market_type is not None else "",
        best_ask=entry_price,
        extras={
            "market_family": descriptor.market_family.value,
            "execution_permission": "auto_execute",
        },
    )
    return ExtensionDecision.buy(
        reason="strategy_entry",
        token_id=token_id,
        price=entry_price,
        amount_usdc=amount_usdc,
        order_type=None,
        post_only=False,
        market_slug=context.market.market_slug,
        metadata=decision_metadata,
        summary=summary,
    )


def _position_entry_price(context: ExtensionContext) -> Decimal | None:
    """估算我方持仓的实际买入均价（cost / shares）。

    动态止盈/止损都以买入均价为基准；无持仓或数据异常（零份额/零成本）时
    返回 None，调用侧退回静态退出价。
    """

    position = context.position
    if position is None or position.shares <= Decimal("0") or position.cost_usdc <= Decimal("0"):
        return None
    return position.cost_usdc / position.shares


def _empty_sizing(context: ExtensionContext, *, reason: str) -> EntrySizing:
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
        config: CurrentStrategyConfig,
        ports: ExtensionPorts | None = None,
    ) -> None:
        self._config = config
        self._ports = ports

    # ---- 主入口 -----------------------------------------------------

    def decide(self, context: ExtensionContext) -> QuantDecision:
        trigger = context.quant_trigger_kind
        if trigger == "market_tick":
            return self._decide_market_tick(context)
        if trigger == "reconcile_cycle":
            return self._decide_reconcile(context)
        return QuantDecision(actions=(), reason=f"unknown_trigger_kind:{trigger}")

    # ---- market_tick：盘口事件路径 ----------------------------------

    def _decide_market_tick(self, context: ExtensionContext) -> QuantDecision:
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

    def _decide_entry_attempt(self, context: ExtensionContext) -> ExtensionDecision:
        """无持仓时的入场决策：按 market family 分派 sizing + 构造 BUY intent。

        - ``OUTRIGHT`` / ``SERIES`` → 走各自子策略（赛季冠军 / 系列赛 winner）
        - ``ESPORTS`` → 直接 skip（不自动交易）
        - ``SINGLE_GAME`` / 未识别 → 走 ``trading.size_entry`` + ``trading.decide_entry``
        """
        from polymarket_trader.quant.outcomes import SportsMarketFamily, describe_sports_market
        from polymarket_trader.quant.outright import (
            decide_outright_entry,
            size_outright_entry,
        )
        from polymarket_trader.quant.series import (
            decide_series_entry,
            size_series_entry,
        )

        if context.market is None or context.orderbook is None:
            return ExtensionDecision.skip(reason="missing_market_state")

        descriptor = describe_sports_market(context.market)
        family = descriptor.market_family

        if family == SportsMarketFamily.ESPORTS:
            return ExtensionDecision.skip(
                reason="esports_not_auto_tradable",
                metadata={"market_family": SportsMarketFamily.ESPORTS.value},
            )

        # family-specific sizer + decider 路由
        if family == SportsMarketFamily.OUTRIGHT:
            sizing = size_outright_entry(self._config, context, self._ports)
        elif family == SportsMarketFamily.SERIES:
            sizing = size_series_entry(self._config, context, self._ports)
        else:
            # SINGLE_GAME 默认路径
            sizing = size_entry(self._config, context)

        if sizing.allocation is None or sizing.allocation.buy_budget_usdc <= Decimal("0"):
            return ExtensionDecision.skip(reason=sizing.reason or "no_allocation")
        focus_context = replace(
            context,
            amount_usdc=sizing.allocation.buy_budget_usdc,
            allocation=sizing.allocation,
            allocation_plan=sizing.allocation_plan,
        )
        if family == SportsMarketFamily.OUTRIGHT:
            return decide_outright_entry(self._config, focus_context, self._ports)
        if family == SportsMarketFamily.SERIES:
            return decide_series_entry(self._config, focus_context, self._ports)
        return decide_entry(self._config, focus_context)

    def _decide_position_action(self, context: ExtensionContext) -> ExtensionDecision:
        from polymarket_trader.quant.position_plan import exit_price_for_context

        config = self._config
        if not config.auto_exit_enabled:
            return ExtensionDecision.skip(reason="settlement_only_exit_disabled")

        now = context.now or _utc_now()
        if context.account_snapshot is not None:
            last_reconcile = context.account_snapshot.last_reconcile_at
            if last_reconcile is None:
                return ExtensionDecision.skip(reason="reconcile_never_completed")
            if last_reconcile.tzinfo is None:
                last_reconcile = last_reconcile.replace(tzinfo=timezone.utc)
            age = (
                now.astimezone(timezone.utc) - last_reconcile.astimezone(timezone.utc)
            ).total_seconds()
            if age > 60.0:
                return ExtensionDecision.skip(
                    reason="reconcile_stale_skip_exit",
                    metadata={"reconcile_age_seconds": str(age)},
                )

        size_shares = context.size_shares
        if size_shares is not None and size_shares > Decimal("0"):
            uncovered_shares = size_shares
        elif context.position is not None:
            uncovered_shares = context.position.shares - context.position.open_sell_shares
        else:
            return ExtensionDecision.skip(reason="missing_position_state")

        if uncovered_shares < Decimal("0.1"):
            replace_decision = _maybe_reprice_stale_sell(config, context, now=now)
            if replace_decision is not None:
                return replace_decision
            return ExtensionDecision.skip(
                reason="no_uncovered_shares",
                metadata={"uncovered_shares": str(uncovered_shares)},
            )

        if (
            context.position is not None
            and (
                context.position.current_value is None
                or context.position.current_value <= Decimal("0")
            )
            and context.orderbook is None
        ):
            return ExtensionDecision.skip(reason="position_zero_value_no_orderbook")

        token_id = (
            context.token_id
            or (context.position.token_id if context.position is not None else None)
            or _metadata_text(context, "token_id")
        )
        entry_price = _position_entry_price(context)
        decision_metadata = build_position_plan_metadata(
            config,
            context,
            token_id=token_id,
            source_reason="quant_exit",
            target_size_shares=uncovered_shares,
            entry_price=entry_price,
        )

        exit_price = exit_price_for_context(config, context, entry_price=entry_price)
        exit_reason = "quant_exit"
        if entry_price is not None:
            dynamic = evaluate_dynamic_exit(
                config,
                context,
                token_id=token_id,
                entry_price=entry_price,
            )
            if dynamic is not None:
                decision_metadata.update(dynamic.metadata)
                if not dynamic.should_exit:
                    return ExtensionDecision.skip(
                        reason=dynamic.reason,
                        metadata=decision_metadata,
                    )
                assert dynamic.exit_price is not None
                exit_price = dynamic.exit_price
                exit_reason = dynamic.reason
                decision_metadata["exit_target_price"] = str(exit_price)
                plan = decision_metadata.get("position_plan")
                if isinstance(plan, dict):
                    plan["target_exit_price"] = str(exit_price)

        if exit_price is not None and context.orderbook is not None:
            tick = context.orderbook.tick_size or Decimal("0.01")
            if tick > Decimal("0"):
                exit_price = (exit_price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        return ExtensionDecision.sell(
            reason=exit_reason,
            token_id=token_id,
            price=exit_price,
            size_shares=uncovered_shares,
            market_slug=(
                context.market.market_slug
                if context.market is not None
                else _metadata_text(context, "market_slug")
            ),
            metadata=decision_metadata,
        )

    # ---- reconcile_cycle：周期路径 ----------------------------------

    def _decide_reconcile(self, context: ExtensionContext) -> QuantDecision:
        from polymarket_trader.quant.recovery import build_recovery_quant_decision
        return build_recovery_quant_decision(self._config, context)
