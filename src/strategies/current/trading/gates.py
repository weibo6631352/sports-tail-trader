"""体育扫尾的入场和分配门禁。

整体由 ``size_entry`` / ``decide_entry`` 调用。这里负责：
- 决策前调用 ``evaluate_tail_opportunity`` / ``evaluate_scale_in_opportunity``
- 处理 manual_confirm、market_end_too_far、scale_in 等分支
- 把评估结果转成可审计 metadata
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.order import OrderSide
from polymarket_trader.extension_api import ExtensionContext, ExtensionDecision

from strategies.current.allocation import AllocationMarketSnapshot
from strategies.current.config import CurrentStrategyConfig, tail_policy_from_config
from strategies.current.outcomes import describe_sports_market
from strategies.current.risk import check_tail_entry_risk
from strategies.current.tail import (
    LiveGameState,
    LiveGameStatus,
    SportsMarketSide,
    SportsMarketSnapshot,
    TailEvaluation,
    TailAction,
    evaluate_scale_in_opportunity,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)

from .helpers import _metadata_decimal, bid_plus_tick_fallback_ask
from .matching import _target_for_live_game
from .pricing import _tail_locked_outcome_signal, _tail_price_cap
from .risk_limits import _buy_fill_summary, _covered_exit_shares


def _tail_entry_gate(
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
    family_metadata = {"market_family": descriptor.market_family.value}
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
    target, target_reason = _target_for_live_game(
        context.market,
        token_id,
        metadata=context.metadata,
        game=game,
    )
    if target is None:
        return (
            ExtensionDecision.skip(
                reason=target_reason,
                metadata={**family_metadata, "parse_reason": descriptor.reason},
            ),
            config.entry_no_price_max,
            {},
        )

    policy = tail_policy_from_config(config)
    locked_outcome_signal = _tail_locked_outcome_signal(context)
    # missing_best_ask fallback：盘口缺 best_ask 但有 best_bid + tick_size 时用
    # bid+tick 估算。让 evaluator 仍跑出 fair_value 对比并产生 RECORD_ONLY 决策
    # ——下单不允许，但避免 single_game 上 96%+ token 在 evaluator 入口 silent
    # drop（§10 拒绝原因可审计 + §9 不静默忽略目标盘口）。fallback metadata 透传
    # 到 decision_records 供 calibration 使用。
    best_ask = context.orderbook.best_ask
    snapshot_metadata: dict[str, str] = {}
    if best_ask is None:
        fallback_ask = bid_plus_tick_fallback_ask(
            context.orderbook,
            context.market.tick_size,
        )
        if fallback_ask is not None:
            best_ask = fallback_ask
            snapshot_metadata = {
                "best_ask_fallback": "bid_plus_tick",
                "best_bid": str(context.orderbook.best_bid),
                "tick_size": str(context.market.tick_size),
                "fallback_ask": str(fallback_ask),
            }
    market_snapshot = SportsMarketSnapshot(
        market_type=descriptor.market_type,
        side=target.side,
        token_id=target.token_id,
        line=descriptor.line,
        best_ask=best_ask,
        buyable_liquidity_usdc=_ask_depth_notional(
            context.orderbook,
            price_cap=_tail_price_cap(
                config,
                context.market,
                token_id,
                locked_outcome_signal=locked_outcome_signal,
            ),
        ),
        market_family=descriptor.market_family,
        market_slug=context.market.market_slug,
        market_end_date=context.market.end_date,
        metadata=snapshot_metadata,
    )
    evaluation = evaluate_tail_opportunity(
        game,
        market_snapshot,
        policy=policy,
        now=context.now,
    )
    metadata = _tail_evaluation_metadata(evaluation)
    metadata.update(family_metadata)
    metadata.update(_tail_confirmation_metadata(context))
    if not evaluation.accepted:
        return (
            ExtensionDecision.skip(reason=evaluation.reason, metadata=metadata),
            market_snapshot.best_ask or config.entry_no_price_max,
            metadata,
        )
    risk_decision = check_tail_entry_risk(
        config,
        market=context.market,
        token_id=token_id,
        buy_budget_usdc=_metadata_decimal(context, "amount_usdc", "buy_budget_usdc") or Decimal("0"),
        candidate_snapshots=_candidate_snapshots_for_gate(context),
        metadata={**context.metadata, **metadata},
        account_snapshot=context.account_snapshot,
        now=context.now,
    )
    metadata.update(risk_decision.metadata or {})
    if not risk_decision.passed:
        return (
            ExtensionDecision.skip(reason=risk_decision.reason, metadata=metadata),
            _tail_price_cap(
                config,
                context.market,
                token_id,
                locked_outcome_signal=locked_outcome_signal,
            ),
            metadata,
        )
    if evaluation.action == TailAction.MANUAL_CONFIRM and _tail_manual_confirmed(context):
        return (
            None,
            _tail_price_cap(
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
                reason=f"tail_{evaluation.action.value}",
                metadata=metadata,
            ),
            _tail_price_cap(
                config,
                context.market,
                token_id,
                locked_outcome_signal=locked_outcome_signal,
            ),
            metadata,
        )
    return (
        None,
        _tail_price_cap(
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

    # 局部 import 避免与 allocation.py 的循环依赖
    from .allocation import _fallback_snapshot

    if context.market is None or context.orderbook is None:
        return None
    snapshot = _fallback_snapshot(context)
    if snapshot is None:
        return None
    buyable_liquidity_usdc = _ask_depth_notional(
        context.orderbook,
        price_cap=_tail_price_cap(config, context.market, snapshot.token_id),
    )
    allowed, metadata, _budget_cap = _scale_in_allocation_gate(
        config,
        context,
        snapshot,
        buyable_liquidity_usdc=buyable_liquidity_usdc,
    )
    if not allowed:
        return None
    return None, _tail_price_cap(config, context.market, snapshot.token_id), metadata


def _tail_allocation_gate(
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
    family_metadata = {"market_family": descriptor.market_family.value}
    if descriptor.market_family.value != "single_game":
        return descriptor.reason, family_metadata
    game = live_game_state_from_metadata(context.metadata)
    target, target_reason = _target_for_live_game(
        snapshot.market,
        snapshot.token_id,
        metadata=context.metadata,
        game=game,
    )
    if target is None:
        return target_reason, {**family_metadata, "parse_reason": descriptor.reason}

    policy = tail_policy_from_config(config)
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
    metadata = _tail_evaluation_metadata(evaluation)
    metadata.update(family_metadata)
    metadata.update(_tail_confirmation_metadata(context))
    if not evaluation.accepted:
        return evaluation.reason, metadata
    if evaluation.action == TailAction.MANUAL_CONFIRM and _tail_manual_confirmed(context):
        return "", metadata
    if evaluation.action != TailAction.AUTO_EXECUTE:
        return f"tail_{evaluation.action.value}", metadata
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
    target, _target_reason = _target_for_live_game(
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
        policy=tail_policy_from_config(config),
        now=context.now,
    )
    if not evaluation.accepted or evaluation.action != TailAction.AUTO_EXECUTE:
        return False, {}, None

    buy_fill_count, first_buy_notional = _buy_fill_summary(context, snapshot)
    if buy_fill_count >= config.tail_scale_in_max_buy_fills:
        return False, {}, None
    budget_cap = first_buy_notional * config.tail_scale_in_budget_fraction
    if budget_cap <= Decimal("0"):
        return False, {}, None

    metadata = _tail_evaluation_metadata(evaluation)
    metadata.update(
        {
            "market_family": descriptor.market_family.value,
            "scale_in_existing_shares": str(position.shares),
            "scale_in_covered_shares": str(covered_shares),
            "scale_in_buy_fill_count": buy_fill_count,
            "scale_in_max_buy_fills": config.tail_scale_in_max_buy_fills,
            "scale_in_budget_cap_usdc": str(budget_cap),
            "allow_open_exit_overlap": True,
        }
    )
    return True, metadata, budget_cap


def _tail_pre_orderbook_skip_reason(
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
        not _tail_can_bypass_market_end_window(config, context, snapshot)
    ):
        model_reject_reason = _tail_static_model_reject_reason(context, snapshot)
        if model_reject_reason:
            return model_reject_reason
        return "market_end_too_far"
    return ""


def _tail_static_model_reject_reason(
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
    from strategies.current.outcomes import target_for_token

    target = target_for_token(snapshot.market, snapshot.token_id)
    if target is not None and target.side == SportsMarketSide.UNDER:
        return "tennis_totals_under_not_supported"
    return ""


def _tail_can_bypass_market_end_window(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
) -> bool:
    """用策略评估判断当前候选是否属于已锁定结果的远期 endDate 豁免。"""

    descriptor = describe_sports_market(snapshot.market)
    if not descriptor.accepted or descriptor.market_type is None:
        return False
    game = live_game_state_from_metadata(context.metadata)
    target, _target_reason = _target_for_live_game(
        snapshot.market,
        snapshot.token_id,
        metadata=context.metadata,
        game=game,
    )
    if game is None or target is None:
        return False
    policy = tail_policy_from_config(config)
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

    if market_end_date is None or config.tail_market_end_horizon_seconds <= 0:
        return False
    current_time = now or datetime.now(timezone.utc)
    market_end = market_end_date
    if market_end.tzinfo is None:
        market_end = market_end.replace(tzinfo=timezone.utc)
    return (market_end.astimezone(timezone.utc) - current_time.astimezone(timezone.utc)).total_seconds() > (
        config.tail_market_end_horizon_seconds
    )


def _has_open_order(snapshot: AllocationMarketSnapshot, side: OrderSide) -> bool:
    """判断当前 token 是否已有开放订单，避免入场计划重复占仓。"""

    return any(order.side == side and order.open for order in snapshot.open_orders)


def _tail_evaluation_metadata(evaluation: TailEvaluation) -> dict[str, object]:
    """把体育扫尾评估结果转换成审计 metadata。"""

    metadata = dict(evaluation.metadata)
    metadata["tail_action"] = evaluation.action.value
    metadata["tail_reason"] = evaluation.reason
    metadata["opportunity_type"] = evaluation.opportunity_type.value
    if evaluation.execution_permission is not None:
        metadata["execution_permission"] = evaluation.execution_permission.value
    return metadata


def _tail_manual_confirmed(context: ExtensionContext) -> bool:
    return context.manual_confirmation is not None


def _tail_confirmation_metadata(context: ExtensionContext) -> dict[str, object]:
    """把 framework 注入的 ManualConfirmation 投影成策略 metadata 字段。

    framework 不再读这些 metadata key（已通过 ``plan.summary.manual_confirmed``
    展示），但策略内部 trading/gates 仍按它们做闸门判断，写回到策略 metadata 供 audit 透传。
    """

    confirmation = context.manual_confirmation
    if confirmation is None:
        return {"manual_confirmed": False}
    return {
        "manual_confirmed": True,
        "confirmed_by": confirmation.operator,
        "confirm_reason": confirmation.reason,
    }


# ---- 内部 ask depth 工具 -------------------------------------------------


def _ask_depth_notional(orderbook, *, price_cap: Decimal | None = None) -> Decimal:
    """计算价格上限内的 ask 侧深度总额。"""

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


def _candidate_snapshots_for_gate(
    context: ExtensionContext,
) -> tuple[AllocationMarketSnapshot, ...]:
    """供入场 gate 调用 risk 时使用的候选快照（局部 import 避免循环依赖）。"""

    from .allocation import _candidate_snapshots
    return _candidate_snapshots(context)
