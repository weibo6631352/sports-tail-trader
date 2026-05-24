"""当前策略的资金分配、入场和退出决策（三个公开 hook）。

不负责远端扫描，也不负责恢复修复语义。所有体育扫尾门禁、价格、profit-take
和风险修正逻辑都委派到子模块。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.kelly import implied_fair_value_from_price_cap
from polymarket_trader.domain.order import OrderSide
from polymarket_trader.extension_api import EntrySizing, ExtensionContext, ExtensionDecision

from strategies.current.allocation import (
    AllocationMarketSnapshot,
    ProbView,
    kelly_plan,
)
from strategies.current.config import CurrentStrategyConfig
from strategies.current.exit_plan import build_exit_plan_metadata, exit_price_for_context
from strategies.current.tail import SportsTailOpportunityType

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
from .exit_overlay import (
    _apply_profit_take_exit_plan,
    _capital_efficiency_gate,
    evaluate_dynamic_exit,
)
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

logger = logging.getLogger(__name__)


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

    from strategies.current.trading.exit_overlay import _estimate_fair_value, _exit_orderbook

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

    from strategies.sports_framework.math_lock import evaluate_math_lock
    from strategies.sports_framework.parsing import live_game_state_from_metadata
    from strategies.current.outcomes import describe_sports_market, target_for_token

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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    # 赔率差价候选的去抽水真实概率视图（按 condition/token 键）。Kelly 必须用
    # 去抽水 true_p 定注，而不是扫尾锁定路径反推的 implied≈1.0——后者会在
    # 概率性入场上严重 over-bet。
    odds_gap_prob_views: dict[tuple[str, str], ProbView] = {}
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
            # 赔率差价候选：把去抽水 true_p 作为 Kelly 的 prob_p。conf=1.0——
            # true_p 来自博彩市场去抽水后的真实概率估计，本身已是市场共识，
            # 不像扫尾 implied 反推那样需要额外抑制不确定性。
            if not skip_reason and tail_metadata.get("opportunity_type") == (
                SportsTailOpportunityType.ODDS_GAP.value
            ):
                true_p_raw = tail_metadata.get("odds_gap_true_p")
                if true_p_raw is not None:
                    odds_gap_prob_views[(snapshot.condition_id, snapshot.token_id)] = ProbView(
                        prob_p=Decimal(str(true_p_raw)),
                        prob_confidence=Decimal("1"),
                        source="odds_gap_devigged",
                    )
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
        """Kelly probability source — 赔率源优先 + 可与数学锁定结合:

        1. Goalserve devig + math_lock 同时可用 → **结合**:p=odds_devig,
           confidence = 0.7 + 0.3 × consistency(odds 和 math 越一致越高 conf)
        2. 只 Goalserve devig: confidence=1.0
        3. 只 math_lock: confidence=0.7
        4. 都没有: fallback implied_fair_value_from_price_cap(confidence 已缩水)

        结合的意义: 用户要求 "有赔率源优先用赔率,可结合数学锁定"。当 Goalserve
        odds 和 math_lock 估计一致(差异 < 5%) → 真信号,confidence 加成;不一致
        → odds 可能 stale,confidence 收紧让 Kelly fraction 变小。
        """

        odds_gap_view = odds_gap_prob_views.get((snap.condition_id, snap.token_id))
        math_view = _math_lock_prob_view(snap, context)

        if odds_gap_view is not None and math_view is not None:
            # 结合: odds_p 作 Kelly p; consistency 调整 confidence
            odds_p = odds_gap_view.prob_p or Decimal("0")
            math_p = math_view.prob_p or Decimal("0")
            diff = abs(odds_p - math_p)
            # diff <= 0.05: 高度一致,conf=1.0; diff >= 0.30: 完全不一致,conf=0.4
            # 线性插值: conf = 1.0 - 2 × diff (clip [0.4, 1.0])
            consistency_conf = max(Decimal("0.4"), min(Decimal("1"), Decimal("1") - Decimal("2") * diff))
            return ProbView(
                prob_p=odds_p,
                prob_confidence=consistency_conf,
                source=f"odds_gap+math_lock(diff={diff:.3f})",
            )

        if odds_gap_view is not None:
            return odds_gap_view

        # math_lock 单源
        if math_view is not None:
            return math_view

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
            entry_price=entry_price,
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

    # 实盘双挂 bug 根因:backend 重启后 in-memory open_orders=[] (未加载),
    # exit overlay 看 position.open_sell_shares=0 算 uncovered=shares 全量,
    # 又挂一笔 SELL → 链上已经有的 SELL(用户手动或前一次实例挂的)被重复。
    # 等 reconcile 完成把链上 open_orders 同步进内存后 open_sell_shares 才对。
    # 这里硬性要求 last_reconcile_at 在 reconcile_freshness_seconds 窗口内才允许
    # 挂 SELL,否则 skip 等下一周期。reconcile 周期 ~30s,给 2x 余量 60s。
    now = context.now or _utc_now()
    if context.account_snapshot is not None:
        last_reconcile = context.account_snapshot.last_reconcile_at
        if last_reconcile is None:
            return ExtensionDecision.skip(reason="reconcile_never_completed")
        if last_reconcile.tzinfo is None:
            last_reconcile = last_reconcile.replace(tzinfo=timezone.utc)
        age = (now.astimezone(timezone.utc) - last_reconcile.astimezone(timezone.utc)).total_seconds()
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
    # position.shares 4 位小数 vs chain open_sell 2 位小数：差值常 < 0.01 视为已覆盖。
    # 这种 sliver 没法挂有效 SELL（min_order_size=5），但旧 SELL 价仍可能 stale 需要
    # reprice，所以走 reprice 检查而非"挂新 SELL"路径。
    min_meaningful_uncovered = Decimal("0.1")
    if uncovered_shares < min_meaningful_uncovered:
        # 全量 size 已被 open SELL 覆盖（或差值过小无法挂新 SELL）。
        # 但 SELL 价位可能 stale (挂 $0.99 等结算，而当前 fair_value 已跌穿)→
        # 检查是否需要 cancel-replace 到 fair_value × 0.97 + entry+offset 取大。
        replace_decision = _maybe_reprice_stale_sell(config, context, now=now)
        if replace_decision is not None:
            return replace_decision
        return ExtensionDecision.skip(
            reason="no_uncovered_shares",
            metadata={"uncovered_shares": str(uncovered_shares)},
        )

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
    # entry_price 先算出来 → 同时喂给 exit_plan metadata 和静态 exit_price，
    # 确保 build_exit_plan_metadata + exit_price_for_context 都走 entry+offset，
    # 而不是 fallback 到 config.exit_no_price ($0.99) 永远等结算。
    entry_price = _position_entry_price(context)
    decision_metadata = build_exit_plan_metadata(
        config,
        context,
        token_id=token_id,
        source_reason="strategy_exit",
        target_size_shares=uncovered_shares,
        entry_price=entry_price,
    )

    # 动态退出：有实时盘口时每个决策周期重估退出价/退出时机，结果优先于
    # 旧的静态 _profit_take_target_price。无盘口时返回 None，退回静态退出价。
    exit_price = exit_price_for_context(config, context, entry_price=entry_price)
    exit_reason = "strategy_exit"
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
                # 本周期 HOLD：不挂 SELL，让头寸继续持有等待更优出价或结算。
                return ExtensionDecision.skip(
                    reason=dynamic.reason,
                    metadata=decision_metadata,
                )
            assert dynamic.exit_price is not None  # should_exit=True 时 exit_price 必有值
            exit_price = dynamic.exit_price
            exit_reason = dynamic.reason
            decision_metadata["exit_target_price"] = str(exit_price)
            plan = decision_metadata.get("exit_plan")
            if isinstance(plan, dict):
                plan["target_exit_price"] = str(exit_price)

    # 强制 floor 到 orderbook.tick_size：所有上游 SELL 价格计算（dynamic exit
    # quantize 到 0.001 / clearing_price 任意精度 / fair × 0.95 等）都可能产出
    # 非 tick 倍数价，进 RiskManager 必被 tick_size_invalid 拒。在 SELL decision
    # 唯一出口统一 floor 是最干净的修复——所有 SELL 路径自动获得保护。
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
            context.market.market_slug if context.market is not None else _metadata_text(context, "market_slug")
        ),
        metadata=decision_metadata,
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
