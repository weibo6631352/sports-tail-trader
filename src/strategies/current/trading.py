"""当前策略的资金分配、入场和退出决策。

这个文件关注的是“拿到市场和账户上下文后，策略怎么做交易决定”，
不负责远端扫描，也不负责恢复修复语义。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Mapping

from polymarket_trader.domain.allocation import (
    Allocation,
    AllocationPlan,
    MarketBuyBudgetChanged,
    current_exposure_usdc,
)
from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.extension_api import EntryCandidate, EntrySizing, ExtensionContext, ExtensionDecision

from strategies.current.allocation import AllocationMarketSnapshot, equal_weight_plan
from strategies.current.config import CurrentStrategyConfig, sports_tail_policy_from_config
from strategies.current.exit_plan import build_exit_plan_metadata
from strategies.current.outcomes import (
    describe_sports_market,
    is_primary_token,
    target_for_token,
)
from strategies.current.risk import check_sports_entry_risk
from strategies.current.sports_tail import (
    SportsMarketSnapshot,
    SportsTailEvaluation,
    TailAction,
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
        price_cap = _sports_tail_price_cap(config, snapshot.market, snapshot.token_id)
        buyable_liquidity_usdc = _ask_depth_notional(
            snapshot.orderbook,
            price_cap=price_cap,
        )
        skip_reason = _allocation_skip_reason(
            config,
            snapshot,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
        )
        if not skip_reason and _is_focus_snapshot(context, snapshot):
            skip_reason, sports_metadata = _sports_tail_allocation_gate(
                config,
                context,
                snapshot,
                buyable_liquidity_usdc=buyable_liquidity_usdc,
            )
            sizing_metadata.update(sports_metadata)
        if skip_reason:
            skipped_allocations[(snapshot.condition_id, snapshot.token_id)] = _skipped_allocation(
                snapshot,
                reason=skip_reason,
            )
            continue
        eligible_snapshots.append(replace(snapshot, liquidity_usdc=buyable_liquidity_usdc))

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

    sports_gate = _sports_tail_entry_gate(config, context)
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

    amount_usdc = context.amount_usdc or _metadata_decimal(context, "amount_usdc", "buy_budget_usdc")
    if amount_usdc is None or amount_usdc <= Decimal("0"):
        return ExtensionDecision.skip(reason="missing_entry_amount")

    token_id = context.token_id or context.orderbook.token_id
    decision_metadata = dict(sports_metadata)
    decision_metadata.update(
        build_exit_plan_metadata(
            config,
            context,
            token_id=token_id,
            source_reason=str(sports_metadata.get("sports_tail_reason") or "strategy_entry"),
        )
    )
    return ExtensionDecision.buy(
        reason="strategy_entry",
        token_id=token_id,
        price=allowed_price,
        amount_usdc=amount_usdc,
        market_slug=context.market.market_slug,
        metadata=decision_metadata,
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
        price=config.exit_no_price,
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
    snapshot: AllocationMarketSnapshot,
    *,
    buyable_liquidity_usdc: Decimal,
) -> str:
    universe_decision = select_market(config, snapshot.market)
    if not universe_decision.selected:
        return universe_decision.reason or "market_out_of_universe"
    if not is_primary_token(snapshot.market, snapshot.token_id):
        return "unsupported_outcome"
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
    if best_ask is None:
        return "missing_best_ask"
    price_cap = _sports_tail_price_cap(config, snapshot.market, snapshot.token_id)
    if best_ask > price_cap:
        return "price_above_entry_max"

    spread = snapshot.spread if snapshot.spread is not None else (
        snapshot.orderbook.spread if snapshot.orderbook is not None else None
    )
    if config.max_spread is not None and spread is not None and spread > config.max_spread:
        return "spread_above_max"

    if buyable_liquidity_usdc < config.min_liquidity_usdc:
        return "liquidity_below_min"

    return ""


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

    token_id = context.token_id or context.orderbook.token_id
    target = target_for_token(context.market, token_id)
    if target is None:
        return (
            ExtensionDecision.skip(
                reason="unsupported_sports_token",
                metadata={"sports_parse_reason": descriptor.reason},
            ),
            config.entry_no_price_max,
            {},
        )

    policy = sports_tail_policy_from_config(config)
    market_snapshot = SportsMarketSnapshot(
        market_type=descriptor.market_type,
        side=target.side,
        token_id=target.token_id,
        line=descriptor.line,
        best_ask=context.orderbook.best_ask,
        buyable_liquidity_usdc=_ask_depth_notional(
            context.orderbook,
            price_cap=_sports_tail_price_cap(config, context.market, token_id),
        ),
        market_slug=context.market.market_slug,
    )
    evaluation = evaluate_tail_opportunity(
        live_game_state_from_metadata(context.metadata),
        market_snapshot,
        policy=policy,
        now=context.now,
    )
    metadata = _sports_tail_evaluation_metadata(evaluation)
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
            _sports_tail_price_cap(config, context.market, token_id),
            metadata,
        )
    if evaluation.action == TailAction.MANUAL_CONFIRM and _sports_tail_manual_confirmed(context.metadata):
        return None, _sports_tail_price_cap(config, context.market, token_id), metadata
    if evaluation.action != TailAction.AUTO_EXECUTE:
        return (
            ExtensionDecision.skip(
                reason=f"sports_tail_{evaluation.action.value}",
                metadata=metadata,
            ),
            _sports_tail_price_cap(config, context.market, token_id),
            metadata,
        )
    return None, _sports_tail_price_cap(config, context.market, token_id), metadata


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
    target = target_for_token(snapshot.market, snapshot.token_id)
    if target is None:
        return "unsupported_sports_token", {"sports_parse_reason": descriptor.reason}

    policy = sports_tail_policy_from_config(config)
    best_ask = snapshot.best_ask if snapshot.best_ask is not None else (
        snapshot.orderbook.best_ask if snapshot.orderbook is not None else None
    )
    evaluation = evaluate_tail_opportunity(
        live_game_state_from_metadata(context.metadata),
        SportsMarketSnapshot(
            market_type=descriptor.market_type,
            side=target.side,
            token_id=target.token_id,
            line=descriptor.line,
            best_ask=best_ask,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
            market_slug=snapshot.market_slug,
        ),
        policy=policy,
        now=context.now,
    )
    metadata = _sports_tail_evaluation_metadata(evaluation)
    metadata.update(_sports_tail_confirmation_metadata(context.metadata))
    if not evaluation.accepted:
        return evaluation.reason, metadata
    if evaluation.action == TailAction.MANUAL_CONFIRM and _sports_tail_manual_confirmed(context.metadata):
        return "", metadata
    if evaluation.action != TailAction.AUTO_EXECUTE:
        return f"sports_tail_{evaluation.action.value}", metadata
    return "", metadata


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
    if evaluation.execution_permission is not None:
        metadata["sports_execution_permission"] = evaluation.execution_permission.value
    return metadata


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
) -> Decimal:
    descriptor = describe_sports_market(market)
    if descriptor.market_type is None:
        return config.entry_no_price_max
    if token_id is not None and target_for_token(market, token_id) is None:
        return config.entry_no_price_max
    if descriptor.market_type.value == "totals":
        return config.sports_totals_max_entry_price
    if descriptor.market_type.value == "moneyline":
        return config.sports_moneyline_max_entry_price
    if descriptor.market_type.value == "spreads":
        return config.sports_spreads_max_entry_price
    return config.entry_no_price_max


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
