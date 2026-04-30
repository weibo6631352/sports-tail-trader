"""当前策略的资金分配、入场和退出决策。

这个文件关注的是“拿到市场和账户上下文后，策略怎么做交易决定”，
不负责远端扫描，也不负责恢复修复语义。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import re
from typing import Any, Mapping

from polymarket_trader.domain.allocation import (
    Allocation,
    AllocationPlan,
    MarketBuyBudgetChanged,
    current_exposure_usdc,
)
from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.domain.order import OrderSide, OrderType
from polymarket_trader.extension_api import EntryCandidate, EntrySizing, ExtensionContext, ExtensionDecision

from strategies.current.allocation import AllocationMarketSnapshot, equal_weight_plan
from strategies.current.config import CurrentStrategyConfig, sports_tail_policy_from_config
from strategies.current.exit_plan import cap_price_to_clob_limit, build_exit_plan_metadata, exit_price_for_context
from strategies.current.outcomes import (
    SportsTokenTarget,
    describe_sports_market,
    is_primary_token,
    target_for_token,
)
from strategies.current.risk import check_sports_entry_risk
from strategies.current.sports_tail import (
    LiveGameState,
    LiveGameStatus,
    SportsMarketSnapshot,
    SportsMarketSide,
    SportsTailEvaluation,
    TailAction,
    evaluate_scale_in_opportunity,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)
from strategies.current.universe import select_market


def size_entry(config: CurrentStrategyConfig, context: ExtensionContext) -> EntrySizing:
    """为当前 market 计算本轮可用入场预算。

    参数：
        config:
            当前策略配置，主要提供价格、深度、spread 等门槛。
        context:
            框架传入的策略上下文。这里依赖其中的市场快照、候选市场列表、
            组合预算和各种上限信息。

    返回：
        ``EntrySizing``，包含：
        - 整体 ``AllocationPlan``
        - 当前目标 market 的 ``Allocation``（如果有）
        - 一个易读的原因字符串

    说明：
        当前默认策略采用“等权分配”。
        也就是说，先找出所有可参与分配的候选市场，再在这些市场之间平均分配预算。
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
        buyable_liquidity_usdc = _maker_bid_liquidity_floor(
            context,
            snapshot,
            price_cap=price_cap,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
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
    """根据盘口和预算生成 BUY 决策。

    参数：
        config:
            当前策略配置，主要使用 ``entry_no_price_max``。
        context:
            当前 market 的策略上下文。要求其中至少有 ``market``、
            ``orderbook``，以及 metadata 中的 ``amount_usdc`` 或
            ``buy_budget_usdc``。

    返回：
        一个 ``ExtensionDecision``：
        - 条件满足时返回 ``BUY``；
        - 条件不足时返回 ``SKIP`` 并说明原因。
    """

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
    missing_ask_maker_bid = _sports_tail_missing_ask_locked_maker_bid(context, sports_metadata, allowed_price)
    if best_ask is None and not missing_ask_maker_bid:
        return ExtensionDecision.skip(reason="missing_best_ask")
    maker_bid_metadata = _sports_tail_locked_maker_bid_metadata(
        context,
        sports_metadata,
        best_ask=best_ask,
        allowed_price=allowed_price,
    )
    if best_ask is not None and best_ask > allowed_price and not maker_bid_metadata:
        return ExtensionDecision.skip(reason="price_above_entry_max")
    entry_price = allowed_price if maker_bid_metadata or missing_ask_maker_bid else best_ask

    amount_usdc = context.amount_usdc or _metadata_decimal(context, "amount_usdc", "buy_budget_usdc")
    if amount_usdc is None or amount_usdc <= Decimal("0"):
        return ExtensionDecision.skip(reason="missing_entry_amount")

    token_id = context.token_id or context.orderbook.token_id
    decision_metadata = dict(sports_metadata)
    decision_metadata.update(missing_ask_maker_bid)
    decision_metadata.update(maker_bid_metadata)
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
        order_type=OrderType.GTC if maker_bid_metadata or missing_ask_maker_bid else None,
        post_only=bool(maker_bid_metadata or missing_ask_maker_bid),
        market_slug=context.market.market_slug,
        metadata=decision_metadata,
    )


def _sports_capital_efficiency_gate(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    entry_price: Decimal,
    amount_usdc: Decimal,
    sports_metadata: Mapping[str, object],
) -> tuple[bool, str, dict[str, object]]:
    """评估体育扫尾入场的预期利润和资金占用效率。

    结算收益足够时允许持有到权威结算，同时如果一档 profit-take SELL 已有
    足够毛利润，也标记买入成交后挂单释放资金；结算效率不足时，只允许这些
    能挂出达标 profit-take SELL 的订单继续进入主链路。
    """

    if "sports_tail_reason" not in sports_metadata:
        return True, "", {}
    if entry_price <= Decimal("0") or entry_price >= Decimal("1"):
        return False, "profit_take_not_viable", {
            "sports_exit_mode": "blocked",
            "sports_capital_efficiency_reason": "entry_price_not_profitable",
        }

    shares = amount_usdc / entry_price
    expected_settlement_profit = shares * (Decimal("1") - entry_price)
    hold_minutes = max(int(config.sports_settlement_hold_minutes), 1)
    expected_profit_per_hour = expected_settlement_profit * Decimal("60") / Decimal(hold_minutes)
    metadata: dict[str, object] = {
        "sports_expected_settlement_profit_usdc": _decimal_metadata_text(expected_settlement_profit),
        "sports_expected_settlement_profit_per_hour_usdc": _decimal_metadata_text(
            expected_profit_per_hour
        ),
        "sports_estimated_settlement_hold_minutes": hold_minutes,
        "sports_min_expected_profit_usdc": str(config.sports_min_expected_profit_usdc),
        "sports_min_expected_profit_per_hour_usdc": str(config.sports_min_expected_profit_per_hour_usdc),
    }
    settlement_efficient = (
        expected_settlement_profit >= config.sports_min_expected_profit_usdc
        and expected_profit_per_hour >= config.sports_min_expected_profit_per_hour_usdc
    )
    if settlement_efficient:
        metadata["sports_exit_mode"] = "settlement"
        profit_take_metadata = _profit_take_metadata(
            config,
            context,
            entry_price=entry_price,
            shares=shares,
        )
        if profit_take_metadata is not None:
            metadata.update(profit_take_metadata)
            metadata["sports_profit_take_overlay_enabled"] = True
        return True, "", metadata

    profit_take_metadata = _profit_take_metadata(
        config,
        context,
        entry_price=entry_price,
        shares=shares,
    )
    if profit_take_metadata is None:
        metadata.update(
            {
                "sports_exit_mode": "blocked",
                "sports_capital_efficiency_reason": "profit_take_target_above_one",
            }
        )
        return False, "profit_take_not_viable", metadata

    metadata.update(profit_take_metadata)
    metadata["sports_exit_mode"] = "profit_take"
    metadata["sports_capital_efficiency_reason"] = "settlement_efficiency_below_min"
    profit_take_profit = Decimal(str(profit_take_metadata["sports_profit_take_expected_profit_usdc"]))
    profit_take_profit_per_hour = Decimal(
        str(profit_take_metadata["sports_profit_take_expected_profit_per_hour_usdc"])
    )
    if profit_take_profit < config.sports_profit_take_min_profit_usdc and (
        profit_take_profit_per_hour < config.sports_min_expected_profit_per_hour_usdc
    ):
        return False, "profit_take_not_viable", metadata
    if profit_take_profit < config.sports_profit_take_min_profit_usdc:
        metadata["sports_capital_efficiency_reason"] = "profit_take_hourly_efficiency_high"
    return True, "", metadata


def _profit_take_metadata(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    entry_price: Decimal,
    shares: Decimal,
) -> dict[str, object] | None:
    """计算一档 profit-take SELL 的审计 metadata。"""

    target_price = _profit_take_target_price(context, entry_price)
    if target_price is None or target_price > Decimal("1"):
        return None
    expected_profit_take_profit = shares * (target_price - entry_price)
    hold_minutes = max(int(config.sports_profit_take_hold_minutes), 1)
    expected_profit_take_profit_per_hour = expected_profit_take_profit * Decimal("60") / Decimal(
        hold_minutes
    )
    return {
        "sports_profit_take_target_price": str(target_price),
        "sports_profit_take_expected_profit_usdc": _decimal_metadata_text(
            expected_profit_take_profit
        ),
        "sports_profit_take_expected_profit_per_hour_usdc": _decimal_metadata_text(
            expected_profit_take_profit_per_hour
        ),
        "sports_profit_take_estimated_hold_minutes": hold_minutes,
        "sports_profit_take_min_profit_usdc": str(config.sports_profit_take_min_profit_usdc),
    }


def _profit_take_target_price(context: ExtensionContext, entry_price: Decimal) -> Decimal | None:
    """返回买入价上方一档 tick 的 profit-take 目标价。"""

    tick_size = _effective_tick_size(context)
    if tick_size is None or tick_size <= Decimal("0"):
        tick_size = Decimal("0.01")
    units = (entry_price / tick_size).to_integral_value(rounding=ROUND_FLOOR)
    target = (units + 1) * tick_size
    if target <= entry_price:
        target += tick_size
    return cap_price_to_clob_limit(target, tick_size=tick_size)


def _effective_tick_size(context: ExtensionContext) -> Decimal | None:
    """读取当前 market 或 orderbook 的最小价格跳动。"""

    if context.orderbook is not None and context.orderbook.tick_size is not None:
        return context.orderbook.tick_size
    if context.market is not None:
        return context.market.tick_size
    return None


def _decimal_metadata_text(value: Decimal) -> str:
    """把审计金额压到稳定小数位，避免无限循环小数撑大 metadata。"""

    return str(value.quantize(Decimal("0.000000000000000001")))


def _apply_profit_take_exit_plan(metadata: dict[str, object]) -> None:
    """把需要主动止盈的订单退出计划改写为买入后挂 profit-take SELL。"""

    if not _has_profit_take_follow_up(metadata):
        return
    target_price = metadata.get("sports_profit_take_target_price")
    metadata["sports_exit_target_price"] = target_price
    plan = metadata.get("sports_exit_plan")
    if not isinstance(plan, dict):
        return
    plan["target_exit_price"] = target_price
    plan["primary_action"] = "place_profit_take_gtc_sell_after_buy_fill"
    plan["settlement_rule"] = "keep_profit_take_order_until_fill_or_authoritative_resolution"
    plan["recovery_rule"] = "cancel_open_entry_orders_and_cover_profit_take_positions"


def _has_profit_take_follow_up(metadata: Mapping[str, object]) -> bool:
    """判断 BUY 成交后是否需要立刻挂一档 profit-take SELL。"""

    return metadata.get("sports_exit_mode") == "profit_take" or bool(
        metadata.get("sports_profit_take_overlay_enabled")
    )


def decide_exit(config: CurrentStrategyConfig, context: ExtensionContext) -> ExtensionDecision:
    """根据持仓状态生成 SELL 决策。

    参数：
        config:
            当前策略配置，主要使用 ``exit_no_price``。
        context:
            当前持仓的策略上下文。可以显式在 metadata 中传
            ``size_shares``，也可以依赖 ``position`` 自动推导未覆盖仓位。

    返回：
        一个 ``ExtensionDecision``：
        - 有可卖份额时返回 ``SELL``；
        - 否则返回 ``SKIP``。
    """

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
    """构造一个“无可分配预算”的占位结果。

    参数：
        context:
            当前策略上下文。
        reason:
            当前无法完成分配的原因。

    返回：
        一个不包含 allocation 的 ``EntrySizing``。
    """

    total_budget_usdc = context.portfolio_budget_usdc or _metadata_decimal(
        context,
        "portfolio_budget_usdc",
    ) or Decimal("0")
    return EntrySizing(
        allocation_plan=AllocationPlan(
            trace_id=context.trace_id,
            total_budget_usdc=total_budget_usdc,
            reason=reason,
        ),
        reason=reason,
    )


def _candidate_snapshots(
    context: ExtensionContext,
) -> tuple[AllocationMarketSnapshot, ...]:
    """从上下文中提取候选市场快照。

    参数：
        context:
            策略上下文，优先使用框架传入的 ``entry_candidates``。

    返回：
        一组 ``AllocationMarketSnapshot``。

    说明：
        正常路径下，框架会把候选市场列表放在 ``ExtensionContext.entry_candidates``。
        如果当前调用点没有提供这个列表，这里会退化为只用当前 market
        生成一个 fallback snapshot，保证逻辑仍可运行。
    """

    if context.entry_candidates:
        return tuple(_entry_candidate_to_snapshot(candidate) for candidate in context.entry_candidates)
    fallback = _fallback_snapshot(context)
    return () if fallback is None else (fallback,)


def _fallback_snapshot(
    context: ExtensionContext,
) -> AllocationMarketSnapshot | None:
    """在缺少候选市场列表时，为当前 market 构造一个最小快照。

    参数：
        context:
            当前策略上下文，要求至少包含 market 和 orderbook。

    返回：
        一个可参与分配计算的 ``AllocationMarketSnapshot``；
        如果上下文信息不足，则返回 ``None``。
    """

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
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
    *,
    buyable_liquidity_usdc: Decimal,
) -> str:
    if _has_open_order(snapshot, OrderSide.BUY):
        return "open_entry_detected"
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
    universe_decision = select_market(config, snapshot.market)
    if not universe_decision.selected:
        return universe_decision.reason or "market_out_of_universe"
    if not is_primary_token(snapshot.market, snapshot.token_id):
        return "unsupported_outcome"
    sports_pre_orderbook_reason = _sports_tail_pre_orderbook_skip_reason(config, context, snapshot)
    if sports_pre_orderbook_reason:
        return sports_pre_orderbook_reason
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

    best_ask = snapshot.best_ask if snapshot.best_ask is not None else (
        snapshot.orderbook.best_ask if snapshot.orderbook is not None else None
    )
    locked_outcome_signal = _sports_tail_locked_outcome_signal(context)
    price_cap = _sports_tail_price_cap(
        config,
        snapshot.market,
        snapshot.token_id,
        locked_outcome_signal=locked_outcome_signal,
    )
    locked_maker_candidate = (
        _sports_tail_maker_bid_signal_from_metadata(context)
        and _is_tennis_set_winner_market(snapshot.market)
        and (
            (best_ask is None and locked_outcome_signal)
            or (best_ask is not None and best_ask <= Decimal("1") and best_ask > price_cap)
        )
    )
    if best_ask is None and not locked_maker_candidate:
        return "missing_best_ask"
    if best_ask is not None and best_ask > price_cap and not locked_maker_candidate:
        return "price_above_entry_max"

    spread = snapshot.spread if snapshot.spread is not None else (
        snapshot.orderbook.spread if snapshot.orderbook is not None else None
    )
    if (
        config.max_spread is not None
        and spread is not None
        and spread > config.max_spread
        and not locked_outcome_signal
    ):
        return "spread_above_max"

    if buyable_liquidity_usdc < config.min_liquidity_usdc and not locked_maker_candidate:
        return "liquidity_below_min"

    return ""


def _sports_tail_pre_orderbook_skip_reason(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
) -> str:
    """执行不依赖盘口 ask 的体育扫尾粗筛。

    远离封盘的 live 市场不应先落成 ``missing_best_ask``，否则候选页会把真正的
    策略拒绝原因隐藏起来。已结束未封盘机会和缺失 end_date 的真实 live 状态继续
    交给后续策略门禁判断。
    """

    descriptor = describe_sports_market(snapshot.market)
    if not descriptor.accepted or descriptor.market_type is None:
        return ""
    if descriptor.market_family.value != "single_game":
        return ""
    game = live_game_state_from_metadata(context.metadata)
    if game is None or game.status == LiveGameStatus.ENDED:
        return ""
    if _market_end_too_far_for_strategy(config, snapshot.market.end_date, now=context.now) and (
        not _sports_tail_can_bypass_market_end_window(config, context, snapshot)
    ):
        model_reject_reason = _sports_tail_static_model_reject_reason(context, snapshot)
        if model_reject_reason:
            return model_reject_reason
        return "market_end_too_far"
    return ""


def _sports_tail_static_model_reject_reason(
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
) -> str:
    """返回不依赖盘口深度和比赛进程的具体模型缺口。

    该判断只用于把粗筛拒绝原因写准确，不放宽任何入场条件。
    """

    game = live_game_state_from_metadata(context.metadata)
    if game is None or not _is_tennis_live_game(game):
        return ""
    descriptor = describe_sports_market(snapshot.market)
    if descriptor.market_type is None or descriptor.market_type.value != "totals":
        return ""
    target = target_for_token(snapshot.market, snapshot.token_id)
    if target is not None and target.side == SportsMarketSide.UNDER:
        return "tennis_totals_under_not_supported"
    return ""


def _sports_tail_can_bypass_market_end_window(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
) -> bool:
    """用策略评估判断当前候选是否属于已锁定结果的远期 endDate 豁免。"""

    descriptor = describe_sports_market(snapshot.market)
    if not descriptor.accepted or descriptor.market_type is None:
        return False
    game = live_game_state_from_metadata(context.metadata)
    target, _target_reason = _sports_target_for_live_game(
        snapshot.market,
        snapshot.token_id,
        metadata=context.metadata,
        game=game,
    )
    if game is None or target is None:
        return False
    policy = sports_tail_policy_from_config(config)
    evaluation = evaluate_tail_opportunity(
        game,
        SportsMarketSnapshot(
            market_type=descriptor.market_type,
            side=target.side,
            token_id=target.token_id,
            line=descriptor.line,
            best_ask=Decimal("0"),
            buyable_liquidity_usdc=max(policy.min_liquidity_usdc, Decimal("1")),
            market_family=descriptor.market_family,
            market_slug=snapshot.market_slug,
            market_end_date=snapshot.market.end_date,
        ),
        policy=policy,
        now=context.now,
    )
    return evaluation.reason != "market_end_too_far"


def _is_tennis_live_game(game: LiveGameState) -> bool:
    return game.tennis_state is not None


def _market_end_too_far_for_strategy(
    config: CurrentStrategyConfig,
    market_end_date: datetime | None,
    *,
    now: datetime | None,
) -> bool:
    """按策略配置判断 market 封盘时间是否仍超出扫尾窗口。"""

    if market_end_date is None or config.sports_market_end_horizon_seconds <= 0:
        return False
    current_time = now or datetime.now(timezone.utc)
    market_end = market_end_date
    if market_end.tzinfo is None:
        market_end = market_end.replace(tzinfo=timezone.utc)
    return (market_end.astimezone(timezone.utc) - current_time.astimezone(timezone.utc)).total_seconds() > (
        config.sports_market_end_horizon_seconds
    )


def _has_open_order(snapshot: AllocationMarketSnapshot, side: OrderSide) -> bool:
    """判断当前 token 是否已有开放订单，避免入场计划重复占仓。"""

    return any(order.side == side and order.open for order in snapshot.open_orders)


def _sports_tail_entry_gate(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> tuple[ExtensionDecision | None, Decimal, dict[str, object]] | None:
    """在下单前执行体育扫尾统一评估门禁。

    返回：
        - ``None``：当前 market 不是可解析体育盘口，交给旧兜底逻辑处理；
        - ``(decision, price, metadata)``：已经完成体育评估。如果 decision 非空，
          调用方直接返回；如果 decision 为空，调用方可继续按返回 price 生成 BUY。
    """

    if context.market is None or context.orderbook is None:
        return None

    descriptor = describe_sports_market(context.market)
    if not descriptor.accepted or descriptor.market_type is None:
        return None
    family_metadata = {"sports_market_family": descriptor.market_family.value}
    if descriptor.market_family.value != "single_game":
        return (
            ExtensionDecision.skip(
                reason=descriptor.reason,
                metadata=family_metadata,
            ),
            config.entry_no_price_max,
            family_metadata,
        )

    token_id = context.token_id or context.orderbook.token_id
    game = live_game_state_from_metadata(context.metadata)
    target, target_reason = _sports_target_for_live_game(
        context.market,
        token_id,
        metadata=context.metadata,
        game=game,
    )
    if target is None:
        return (
            ExtensionDecision.skip(
                reason=target_reason,
                metadata={**family_metadata, "sports_parse_reason": descriptor.reason},
            ),
            config.entry_no_price_max,
            {},
        )

    policy = sports_tail_policy_from_config(config)
    locked_outcome_signal = _sports_tail_locked_outcome_signal(context)
    market_snapshot = SportsMarketSnapshot(
        market_type=descriptor.market_type,
        side=target.side,
        token_id=target.token_id,
        line=descriptor.line,
        best_ask=context.orderbook.best_ask,
        buyable_liquidity_usdc=_ask_depth_notional(
            context.orderbook,
            price_cap=_sports_tail_price_cap(
                config,
                context.market,
                token_id,
                locked_outcome_signal=locked_outcome_signal,
            ),
        ),
        market_family=descriptor.market_family,
        market_slug=context.market.market_slug,
        market_end_date=context.market.end_date,
    )
    evaluation = evaluate_tail_opportunity(
        game,
        market_snapshot,
        policy=policy,
        now=context.now,
    )
    metadata = _sports_tail_evaluation_metadata(evaluation)
    metadata.update(family_metadata)
    metadata.update(_sports_tail_confirmation_metadata(context.metadata))
    if not evaluation.accepted:
        return (
            ExtensionDecision.skip(reason=evaluation.reason, metadata=metadata),
            market_snapshot.best_ask or config.entry_no_price_max,
            metadata,
        )
    risk_decision = check_sports_entry_risk(
        config,
        market=context.market,
        token_id=token_id,
        buy_budget_usdc=_metadata_decimal(context, "amount_usdc", "buy_budget_usdc") or Decimal("0"),
        candidate_snapshots=_candidate_snapshots(context),
        metadata={**context.metadata, **metadata},
        account_snapshot=context.account_snapshot,
        now=context.now,
    )
    metadata.update(risk_decision.metadata or {})
    if not risk_decision.passed:
        return (
            ExtensionDecision.skip(reason=risk_decision.reason, metadata=metadata),
            _sports_tail_price_cap(
                config,
                context.market,
                token_id,
                locked_outcome_signal=locked_outcome_signal,
            ),
            metadata,
        )
    if evaluation.action == TailAction.MANUAL_CONFIRM and _sports_tail_manual_confirmed(context.metadata):
        return (
            None,
            _sports_tail_price_cap(
                config,
                context.market,
                token_id,
                locked_outcome_signal=locked_outcome_signal,
            ),
            metadata,
        )
    if evaluation.action != TailAction.AUTO_EXECUTE:
        return (
            ExtensionDecision.skip(
                reason=f"sports_tail_{evaluation.action.value}",
                metadata=metadata,
            ),
            _sports_tail_price_cap(
                config,
                context.market,
                token_id,
                locked_outcome_signal=locked_outcome_signal,
            ),
            metadata,
        )
    return (
        None,
        _sports_tail_price_cap(
            config,
            context.market,
            token_id,
            locked_outcome_signal=locked_outcome_signal,
        ),
        metadata,
    )


def _scale_in_entry_gate(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> tuple[ExtensionDecision | None, Decimal, dict[str, object]] | None:
    """判断当前入场请求是否属于已有持仓的受控加仓。"""

    if context.market is None or context.orderbook is None:
        return None
    snapshot = _fallback_snapshot(context)
    if snapshot is None:
        return None
    buyable_liquidity_usdc = _ask_depth_notional(
        context.orderbook,
        price_cap=_sports_tail_price_cap(config, context.market, snapshot.token_id),
    )
    allowed, metadata, _budget_cap = _scale_in_allocation_gate(
        config,
        context,
        snapshot,
        buyable_liquidity_usdc=buyable_liquidity_usdc,
    )
    if not allowed:
        return None
    return None, _sports_tail_price_cap(config, context.market, snapshot.token_id), metadata


def _sports_tail_allocation_gate(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
    *,
    buyable_liquidity_usdc: Decimal,
) -> tuple[str, dict[str, object]]:
    """在预算分配阶段执行当前焦点候选的体育扫尾门禁。"""

    descriptor = describe_sports_market(snapshot.market)
    if not descriptor.accepted or descriptor.market_type is None:
        return "", {}
    family_metadata = {"sports_market_family": descriptor.market_family.value}
    if descriptor.market_family.value != "single_game":
        return descriptor.reason, family_metadata
    game = live_game_state_from_metadata(context.metadata)
    target, target_reason = _sports_target_for_live_game(
        snapshot.market,
        snapshot.token_id,
        metadata=context.metadata,
        game=game,
    )
    if target is None:
        return target_reason, {**family_metadata, "sports_parse_reason": descriptor.reason}

    policy = sports_tail_policy_from_config(config)
    best_ask = snapshot.best_ask if snapshot.best_ask is not None else (
        snapshot.orderbook.best_ask if snapshot.orderbook is not None else None
    )
    evaluation = evaluate_tail_opportunity(
        game,
        SportsMarketSnapshot(
            market_type=descriptor.market_type,
            side=target.side,
            token_id=target.token_id,
            line=descriptor.line,
            best_ask=best_ask,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
            market_family=descriptor.market_family,
            market_slug=snapshot.market_slug,
            market_end_date=snapshot.market.end_date,
        ),
        policy=policy,
        now=context.now,
    )
    metadata = _sports_tail_evaluation_metadata(evaluation)
    metadata.update(family_metadata)
    metadata.update(_sports_tail_confirmation_metadata(context.metadata))
    if not evaluation.accepted:
        return evaluation.reason, metadata
    if evaluation.action == TailAction.MANUAL_CONFIRM and _sports_tail_manual_confirmed(context.metadata):
        return "", metadata
    if evaluation.action != TailAction.AUTO_EXECUTE:
        return f"sports_tail_{evaluation.action.value}", metadata
    return "", metadata


def _scale_in_allocation_gate(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
    *,
    buyable_liquidity_usdc: Decimal,
) -> tuple[bool, dict[str, object], Decimal | None]:
    """返回当前候选是否允许按受控加仓参与预算分配。"""

    position = snapshot.position
    if position is None or position.shares <= Decimal("0"):
        return False, {}, None
    if _has_open_order(snapshot, OrderSide.BUY):
        return False, {}, None
    covered_shares = _covered_exit_shares(snapshot)
    if config.auto_exit_enabled and covered_shares < position.shares:
        return False, {}, None

    descriptor = describe_sports_market(snapshot.market)
    if not descriptor.accepted or descriptor.market_type is None:
        return False, {}, None
    if descriptor.market_family.value != "single_game":
        return False, {}, None
    game = live_game_state_from_metadata(context.metadata)
    target, _target_reason = _sports_target_for_live_game(
        snapshot.market,
        snapshot.token_id,
        metadata=context.metadata,
        game=game,
    )
    if target is None:
        return False, {}, None

    best_ask = snapshot.best_ask if snapshot.best_ask is not None else (
        snapshot.orderbook.best_ask if snapshot.orderbook is not None else None
    )
    evaluation = evaluate_scale_in_opportunity(
        game,
        SportsMarketSnapshot(
            market_type=descriptor.market_type,
            side=target.side,
            token_id=target.token_id,
            line=descriptor.line,
            best_ask=best_ask,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
            market_family=descriptor.market_family,
            market_slug=snapshot.market_slug,
            market_end_date=snapshot.market.end_date,
        ),
        policy=sports_tail_policy_from_config(config),
        now=context.now,
    )
    if not evaluation.accepted or evaluation.action != TailAction.AUTO_EXECUTE:
        return False, {}, None

    buy_fill_count, first_buy_notional = _buy_fill_summary(context, snapshot)
    if buy_fill_count >= config.sports_scale_in_max_buy_fills:
        return False, {}, None
    budget_cap = first_buy_notional * config.sports_scale_in_budget_fraction
    if budget_cap <= Decimal("0"):
        return False, {}, None

    metadata = _sports_tail_evaluation_metadata(evaluation)
    metadata.update(
        {
            "sports_market_family": descriptor.market_family.value,
            "sports_scale_in_existing_shares": str(position.shares),
            "sports_scale_in_covered_shares": str(covered_shares),
            "sports_scale_in_buy_fill_count": buy_fill_count,
            "sports_scale_in_max_buy_fills": config.sports_scale_in_max_buy_fills,
            "sports_scale_in_budget_cap_usdc": str(budget_cap),
            "allow_open_exit_overlap": True,
        }
    )
    return True, metadata, budget_cap


def _sports_market_skip_metadata(
    snapshot: AllocationMarketSnapshot,
    reason: str,
) -> dict[str, object]:
    """把 universe / allocation 早期跳过原因补成候选可读的体育审计字段。"""

    descriptor = describe_sports_market(snapshot.market)
    metadata: dict[str, object] = {
        "sports_tail_action": "reject",
        "sports_tail_reason": reason,
        "sports_market_family": descriptor.market_family.value,
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


def _sports_target_for_live_game(
    market,
    token_id: str | None,
    *,
    metadata: Mapping[str, Any],
    game: LiveGameState | None,
) -> tuple[SportsTokenTarget | None, str]:
    """用直播源主客队修正 side token 的 HOME/AWAY 方向。

    Polymarket 体育 market 的 outcomes 顺序不稳定，尤其常见“客队 vs 主队”的
    展示顺序。只在有直播比赛状态时按队名重映射方向；没有直播状态时保留
    descriptor 的静态结果，让历史回放和非体育兜底路径不被误伤。
    """

    target = target_for_token(market, token_id)
    if target is None:
        return None, "unsupported_sports_token"
    if target.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return target, ""
    if game is None:
        return target, ""

    live_side = _live_side_for_outcome_label(target.label, game=game, metadata=metadata)
    if live_side is None:
        return None, "sports_token_live_side_mismatch"
    return replace(target, side=live_side), ""


def _live_side_for_outcome_label(
    label: str,
    *,
    game: LiveGameState,
    metadata: Mapping[str, Any],
) -> SportsMarketSide | None:
    """根据 token 文案判断它对应直播源的主队还是客队。"""

    label_text = _normalize_competitor_text(label)
    if not label_text:
        return None

    match_metadata = metadata.get("sports_live_match")
    match_mapping = match_metadata if isinstance(match_metadata, Mapping) else {}
    home_aliases = _competitor_aliases(
        game.home_name,
        match_mapping.get("matched_home_alias"),
        match_mapping.get("home_name"),
    )
    away_aliases = _competitor_aliases(
        game.away_name,
        match_mapping.get("matched_away_alias"),
        match_mapping.get("away_name"),
    )
    home_score = _label_alias_score(label_text, home_aliases)
    away_score = _label_alias_score(label_text, away_aliases)
    if home_score > away_score:
        return SportsMarketSide.HOME
    if away_score > home_score:
        return SportsMarketSide.AWAY
    return None


def _competitor_aliases(*values: object) -> set[str]:
    """生成队名匹配别名，兼容全名、队名末尾和较长单词 token。"""

    aliases: set[str] = set()
    for value in values:
        normalized = _normalize_competitor_text(value)
        if not normalized:
            continue
        tokens = tuple(token for token in normalized.split() if token not in _GENERIC_COMPETITOR_TOKENS)
        if not tokens:
            continue
        phrase = " ".join(tokens)
        aliases.add(phrase)
        if len(tokens) >= 2:
            aliases.add(tokens[-1])
        aliases.update(token for token in tokens if len(token) >= 3)
    return aliases


def _label_alias_score(label_text: str, aliases: set[str]) -> int:
    """返回 outcome 文案命中一侧别名的强度，完整队名会强于共享地区词。"""

    padded = f" {label_text} "
    return max(
        (
            len(alias.replace(" ", ""))
            for alias in aliases
            if alias and (label_text == alias or f" {alias} " in padded)
        ),
        default=0,
    )


def _normalize_competitor_text(value: object) -> str:
    text = str(value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


_GENERIC_COMPETITOR_TOKENS = {
    "a",
    "an",
    "and",
    "at",
    "club",
    "fc",
    "game",
    "match",
    "men",
    "team",
    "the",
    "to",
    "vs",
    "v",
    "women",
}


def _apply_sports_risk_limits(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
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
        risk_decision = check_sports_entry_risk(
            config,
            market=snapshot.market,
            token_id=snapshot.token_id,
            buy_budget_usdc=allocation.buy_budget_usdc,
            candidate_snapshots=candidate_snapshots,
            metadata=context.metadata,
            account_snapshot=context.account_snapshot,
            now=context.now,
        )
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
            reason=plan.reason if plan.reason else "sports_risk_limited",
        ),
        focus_metadata,
    )


def _sports_tail_evaluation_metadata(evaluation: SportsTailEvaluation) -> dict[str, object]:
    """把体育扫尾评估结果转换成审计 metadata。"""

    metadata = dict(evaluation.metadata)
    metadata["sports_tail_action"] = evaluation.action.value
    metadata["sports_tail_reason"] = evaluation.reason
    metadata["sports_tail_opportunity_type"] = evaluation.opportunity_type.value
    if evaluation.execution_permission is not None:
        metadata["sports_execution_permission"] = evaluation.execution_permission.value
    return metadata


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
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
) -> tuple[int, Decimal]:
    """统计当前 token 的 BUY 成交次数和首笔入场金额，用于限制加仓。"""

    fills = tuple(getattr(context.account_snapshot, "fills", ()) or ())
    buy_fills = [
        fill
        for fill in fills
        if getattr(fill, "condition_id", None) == snapshot.condition_id
        and getattr(fill, "token_id", None) == snapshot.token_id
        and str(getattr(fill, "side", "") or "").upper() == "BUY"
    ]
    if not buy_fills:
        fallback_notional = Decimal("0") if snapshot.position is None else snapshot.position.cost_usdc
        return (1 if snapshot.position is not None else 0), fallback_notional
    first_notional = _fill_notional_usdc(buy_fills[0])
    if first_notional <= Decimal("0") and snapshot.position is not None:
        first_notional = snapshot.position.cost_usdc
    return len(buy_fills), first_notional


def _sports_tail_manual_confirmed(metadata: Mapping[str, object]) -> bool:
    return bool(metadata.get("sports_tail_manual_confirmed"))


def _sports_tail_confirmation_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    confirmed = _sports_tail_manual_confirmed(metadata)
    result: dict[str, object] = {"sports_tail_manual_confirmed": confirmed}
    for key in ("sports_tail_confirmed_by", "sports_tail_confirm_reason"):
        value = metadata.get(key)
        if value:
            result[key] = value
    return result


def _is_focus_snapshot(
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
) -> bool:
    """判断 allocation snapshot 是否对应当前触发入场判断的 token。"""

    if context.market is None:
        return False
    focus_token_id = context.token_id or (context.orderbook.token_id if context.orderbook is not None else None)
    return snapshot.condition_id == context.market.condition_id and snapshot.token_id == focus_token_id


def _sports_tail_price_cap(
    config: CurrentStrategyConfig,
    market,
    token_id: str | None,
    *,
    locked_outcome_signal: bool = False,
) -> Decimal:
    descriptor = describe_sports_market(market)
    if descriptor.market_type is None:
        return config.entry_no_price_max
    if token_id is not None and target_for_token(market, token_id) is None:
        return config.entry_no_price_max
    if locked_outcome_signal and descriptor.market_type.value == "moneyline" and _is_tennis_set_winner_market(market):
        return config.sports_tennis_locked_moneyline_max_entry_price
    if descriptor.market_type.value == "totals":
        return config.sports_totals_max_entry_price
    if descriptor.market_type.value == "moneyline":
        return config.sports_moneyline_max_entry_price
    if descriptor.market_type.value == "spreads":
        return config.sports_spreads_max_entry_price
    return config.entry_no_price_max


def _sports_tail_locked_outcome_signal(context: ExtensionContext) -> bool:
    """判断 live-state 是否已经标记当前市场为数学锁定候选。"""

    return str(context.metadata.get("sports_tail_entry_signal_reason") or "") == "live_outcome_lock_candidate"


def _sports_tail_maker_bid_signal_from_metadata(context: ExtensionContext) -> bool:
    """判断 live-state 粗信号是否允许后续评估考虑 maker bid。"""

    return str(context.metadata.get("sports_tail_entry_signal_reason") or "") in {
        "live_outcome_lock_candidate",
        "live_tail_state_candidate",
    }


def _sports_tail_near_lock_maker_signal(
    context: ExtensionContext,
    sports_metadata: Mapping[str, object],
) -> bool:
    """判断当前盘口是否允许用 1 以下 maker bid 追近锁定网球盘。"""

    return (
        str(context.metadata.get("sports_tail_entry_signal_reason") or "") == "live_tail_state_candidate"
        and sports_metadata.get("sports_tail_reason") == "tennis_set_winner_current_set_near_locked"
    )


def _sports_tail_maker_bid_signal(
    context: ExtensionContext,
    sports_metadata: Mapping[str, object],
) -> bool:
    return (
        _sports_tail_locked_outcome_signal(context)
        and sports_metadata.get("sports_tail_reason") == "tennis_set_winner_locked"
    ) or _sports_tail_near_lock_maker_signal(context, sports_metadata)


def _sports_tail_locked_maker_bid_metadata(
    context: ExtensionContext,
    sports_metadata: Mapping[str, object],
    *,
    best_ask: Decimal | None,
    allowed_price: Decimal,
) -> dict[str, object]:
    """返回锁定结果以盈利价格挂 maker bid 的审计 metadata。"""

    if best_ask is None or not (allowed_price < best_ask <= Decimal("1")):
        return {}
    if _sports_tail_locked_outcome_signal(context) and sports_metadata.get("sports_tail_reason") == (
        "tennis_set_winner_locked"
    ):
        return {
            "sports_tail_maker_bid_reason": "ask_above_locked_price_cap",
            "sports_tail_observed_best_ask": str(best_ask),
            "sports_tail_order_price_cap": str(allowed_price),
        }
    if _sports_tail_near_lock_maker_signal(context, sports_metadata):
        return {
            "sports_tail_maker_bid_reason": "ask_above_near_lock_price_cap",
            "sports_tail_observed_best_ask": str(best_ask),
            "sports_tail_order_price_cap": str(allowed_price),
        }
    return {}


def _sports_tail_missing_ask_locked_maker_bid(
    context: ExtensionContext,
    sports_metadata: Mapping[str, object],
    allowed_price: Decimal,
) -> dict[str, object]:
    """返回锁定结果缺 ask 时主动挂盈利 bid 的审计 metadata。"""

    if (
        _sports_tail_locked_outcome_signal(context)
        and sports_metadata.get("sports_tail_reason") == "tennis_set_winner_locked"
        and allowed_price == context.orderbook.best_bid
    ):
        return {}
    if _sports_tail_locked_outcome_signal(context) and sports_metadata.get("sports_tail_reason") == (
        "tennis_set_winner_locked"
    ):
        return {
            "sports_tail_maker_bid_reason": "missing_best_ask_locked_outcome",
            "sports_tail_order_price_cap": str(allowed_price),
        }
    return {}


def _maker_bid_liquidity_floor(
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
    *,
    price_cap: Decimal,
    buyable_liquidity_usdc: Decimal,
) -> Decimal:
    """给锁定结果的被动买单提供分配预算下限。

    这类订单不是吃当前 ask，而是在 1 以下挂盈利 bid，因此不能用 ask 深度为 0
    直接推导为不可分配。
    """

    best_ask = snapshot.best_ask if snapshot.best_ask is not None else (
        snapshot.orderbook.best_ask if snapshot.orderbook is not None else None
    )
    if not (
        _sports_tail_maker_bid_signal_from_metadata(context)
        and _is_tennis_set_winner_market(snapshot.market)
        and (
            (best_ask is None and _sports_tail_locked_outcome_signal(context))
            or (best_ask is not None and price_cap < best_ask <= Decimal("1"))
        )
    ):
        return buyable_liquidity_usdc
    min_order_budget = snapshot.market.min_order_size * (best_ask or price_cap)
    requested_budget = context.amount_usdc or _metadata_decimal(context, "amount_usdc", "buy_budget_usdc")
    floor = requested_budget if requested_budget is not None and requested_budget > min_order_budget else min_order_budget
    return floor if floor > buyable_liquidity_usdc else buyable_liquidity_usdc


def _is_tennis_set_winner_market(market) -> bool:
    text = str(getattr(market, "market_slug", "") or "").strip().lower().replace("_", " ").replace("-", " ")
    return "set winner" in text or "first set winner" in text


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


def _ask_depth_notional(orderbook, *, price_cap: Decimal | None = None) -> Decimal:
    """计算价格上限内的 ask 侧深度总额。

    参数：
        orderbook:
            当前盘口快照。
        price_cap:
            可选价格上限。未提供时统计全部 ask 深度。

    返回：
        在价格上限内可立即成交的深度总额，单位 USDC。
    """

    if orderbook is None:
        return Decimal("0")
    depth_usdc = Decimal("0")
    levels = orderbook.asks
    if not levels and orderbook.best_ask is not None and orderbook.best_ask_size is not None:
        if price_cap is None or orderbook.best_ask <= price_cap:
            return orderbook.best_ask * orderbook.best_ask_size
        return Decimal("0")
    for level in levels:
        if price_cap is None or level.price <= price_cap:
            depth_usdc += level.price * level.size
    return depth_usdc


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


def _fill_notional_usdc(fill: object) -> Decimal:
    notional = getattr(fill, "notional_usdc", None)
    if notional is not None:
        try:
            return Decimal(str(notional))
        except Exception:
            return Decimal("0")
    price = getattr(fill, "price", None)
    size = getattr(fill, "size", None)
    if price is None or size is None:
        return Decimal("0")
    try:
        return Decimal(str(price)) * Decimal(str(size))
    except Exception:
        return Decimal("0")


def _sizing_reason(plan: AllocationPlan, allocation: Allocation | None) -> str:
    """返回对外展示时更有解释力的 sizing 原因。

    优先使用当前 allocation 的具体原因；
    如果当前 market 没有单独原因，再退回整体 plan 的原因。
    """

    if allocation is not None and allocation.reason:
        return allocation.reason
    return plan.reason


def _metadata_decimal(context: ExtensionContext, *keys: str) -> Decimal | None:
    """按优先顺序从 metadata 中读取十进制数值。

    参数：
        context:
            当前策略上下文。
        *keys:
            允许尝试的 metadata key 列表。会按传入顺序依次尝试。

    返回：
        解析成功时返回 ``Decimal``，否则返回 ``None``。
    """

    for key in keys:
        value = context.metadata.get(key)
        if value is None:
            continue
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            return None
    return None


def _metadata_text(context: ExtensionContext, *keys: str) -> str | None:
    """按优先顺序从 metadata 中读取文本值。"""

    for key in keys:
        value = context.metadata.get(key)
        if value is not None:
            return str(value)
    return None
