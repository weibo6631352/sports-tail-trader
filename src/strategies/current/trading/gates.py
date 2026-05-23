"""体育扫尾的入场和分配门禁。

整体由 ``size_entry`` / ``decide_entry`` 调用。这里负责：
- 决策前调用 ``evaluate_tail_opportunity`` / ``evaluate_scale_in_opportunity``
- 处理 manual_confirm、market_end_too_far、scale_in 等分支
- 把评估结果转成可审计 metadata
"""

from __future__ import annotations

import dataclasses
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
    SportsMarketFamily,
    SportsMarketSide,
    SportsMarketSnapshot,
    TailEvaluation,
    TailAction,
    evaluate_scale_in_opportunity,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)

from .helpers import (
    bid_plus_tick_fallback_ask,
    bid_plus_tick_fallback_metadata,
)
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
    if descriptor.market_family != SportsMarketFamily.SINGLE_GAME:
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

    # 已结束但开赛时间太短 → Goalserve 把另一场（如双赛程午场）的终局比分错配到本场。
    # 任何运动完成一场正式比赛都需要至少 60 分钟；棒球/篮球/足球通常 ≥ 90 分钟。
    # 状态冲突时 status 字段可能被解析为 "live"，但 period="Finished" 同样意味着比赛已结束。
    if game is not None and _is_game_ended(game) and context.market.game_start_time is not None:
        now_dt = (context.now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        market_start = context.market.game_start_time
        if market_start.tzinfo is None:
            market_start = market_start.replace(tzinfo=timezone.utc)
        elapsed_seconds = (now_dt - market_start.astimezone(timezone.utc)).total_seconds()
        min_duration = _min_game_duration_seconds(game.league)
        if elapsed_seconds < min_duration:
            meta = {
                **family_metadata,
                "impossible_elapsed_seconds": round(elapsed_seconds),
                "min_game_duration_seconds": min_duration,
                "game_league": game.league,
            }
            return (
                ExtensionDecision.skip(reason="impossible_game_duration", metadata=meta),
                config.entry_no_price_max,
                meta,
            )

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
    # Goalserve Moneyline 交叉验证：Goalserve 认为目标方概率远低于 Polymarket ask 时拒绝。
    gs_veto = _goalserve_moneyline_veto(config, context, target.side)
    if gs_veto is not None:
        return (
            ExtensionDecision.skip(reason="goalserve_cross_validation_veto", metadata={**family_metadata, **gs_veto}),
            config.entry_no_price_max,
            {**family_metadata, **gs_veto},
        )
    # Goalserve 半场 Money Line 否决：剩余比赛书商定价显示我方胜率极低时拒绝。
    ht_veto = _goalserve_halftime_veto(config, context, target.side)
    if ht_veto is not None:
        return (
            ExtensionDecision.skip(reason="goalserve_halftime_veto", metadata={**family_metadata, **ht_veto}),
            config.entry_no_price_max,
            {**family_metadata, **ht_veto},
        )
    # Goalserve 多信号策略调参：Spread 冲突 → 收紧领先要求；多信号确认 → 放宽领先 + 扩时窗。
    gs_policy_adj = _goalserve_policy_adjustments(config, context, target.side)
    if gs_policy_adj:
        policy = dataclasses.replace(policy, **gs_policy_adj)
    # Goalserve 强确认信号：Goalserve 大幅看好目标方时允许 price_cap 小幅加成，
    # 争取在稍高价格层仍能成交，同时保留完整的审计 metadata。
    gs_edge = _goalserve_strong_edge_signal(config, context, target.side)
    # Goalserve Spread 方向冲突检测（写 metadata，决策已在 policy_adjustments 中体现）。
    gs_spread_conflict = _goalserve_spread_conflict_signal(config, context, target.side)

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
            snapshot_metadata = bid_plus_tick_fallback_metadata(
                orderbook=context.orderbook,
                tick_size=context.market.tick_size,
                fallback_ask=fallback_ask,
            )
    base_price_cap = _tail_price_cap(
        config,
        context.market,
        token_id,
        locked_outcome_signal=locked_outcome_signal,
    )
    # 强确认时 price_cap 加成：允许在稍高价位层多买一点流动性。
    price_cap = base_price_cap
    if (
        gs_edge.get("goalserve_edge_confirmed")
        and config.goalserve_strong_edge_enabled
        and config.goalserve_strong_edge_price_bonus > 0
        and price_cap is not None
    ):
        bonus = Decimal(str(gs_edge["goalserve_edge_price_bonus"]))
        price_cap = min(price_cap + bonus, Decimal("0.99"))

    market_snapshot = SportsMarketSnapshot(
        market_type=descriptor.market_type,
        side=target.side,
        token_id=target.token_id,
        line=descriptor.line,
        best_ask=best_ask,
        best_bid=context.orderbook.best_bid,
        buyable_liquidity_usdc=_ask_depth_notional(
            context.orderbook,
            price_cap=price_cap,
        ),
        market_family=descriptor.market_family,
        market_slug=context.market.market_slug,
        sports_market_type=descriptor.sports_market_type,
        market_end_date=context.market.end_date,
        metadata={**snapshot_metadata, **_goalserve_odds_metadata(context)},
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
    metadata.update(gs_edge)
    metadata.update(gs_spread_conflict)
    if gs_policy_adj:
        metadata["goalserve_policy_adjustments"] = gs_policy_adj
    if not evaluation.accepted:
        return (
            ExtensionDecision.skip(reason=evaluation.reason, metadata=metadata),
            market_snapshot.best_ask or config.entry_no_price_max,
            metadata,
        )
    risk_decision = check_tail_entry_risk(
        config,
        metadata={**context.metadata, **metadata},
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
    if descriptor.market_family != SportsMarketFamily.SINGLE_GAME:
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
    # Goalserve 多信号调参与 entry gate 保持一致，避免 allocation gate 用基础 policy
    # 过滤掉 Goalserve 确认后可放宽条件才能通过的候选（否则永远到不了 entry gate）。
    gs_policy_adj = _goalserve_policy_adjustments(config, context, target.side)
    if gs_policy_adj:
        policy = dataclasses.replace(policy, **gs_policy_adj)
    evaluation = evaluate_tail_opportunity(
        game,
        SportsMarketSnapshot(
            market_type=descriptor.market_type,
            side=target.side,
            token_id=target.token_id,
            line=descriptor.line,
            best_ask=snapshot.effective_best_ask,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
            market_family=descriptor.market_family,
            market_slug=snapshot.market_slug,
            sports_market_type=descriptor.sports_market_type,
            market_end_date=snapshot.market.end_date,
            metadata=_goalserve_odds_metadata(context),
        ),
        policy=policy,
        now=context.now,
    )
    metadata = _tail_evaluation_metadata(evaluation)
    metadata.update(family_metadata)
    metadata.update(_tail_confirmation_metadata(context))
    if gs_policy_adj:
        metadata["goalserve_policy_adjustments"] = gs_policy_adj
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
    if descriptor.market_family != SportsMarketFamily.SINGLE_GAME:
        return False, {}, None
    game = live_game_state_from_metadata(context.metadata)
    # ENDED 比赛不走 scale-in 路径，避免比赛结束后持续触发加仓决策循环。
    # Post-game ended-not-closed 买入由主入场路径（evaluate_tail_opportunity）处理。
    if game is not None and game.status == LiveGameStatus.ENDED:
        return False, {}, None
    target, _target_reason = _target_for_live_game(
        snapshot.market,
        snapshot.token_id,
        metadata=context.metadata,
        game=game,
    )
    if target is None:
        return False, {}, None

    # 加仓也受 Goalserve 信号否决：Goalserve 显示胜率极低时不追加暴露。
    gs_veto = _goalserve_moneyline_veto(config, context, target.side)
    if gs_veto is not None:
        return False, {}, None
    ht_veto = _goalserve_halftime_veto(config, context, target.side)
    if ht_veto is not None:
        return False, {}, None

    evaluation = evaluate_scale_in_opportunity(
        game,
        SportsMarketSnapshot(
            market_type=descriptor.market_type,
            side=target.side,
            token_id=target.token_id,
            line=descriptor.line,
            best_ask=snapshot.effective_best_ask,
            buyable_liquidity_usdc=buyable_liquidity_usdc,
            market_family=descriptor.market_family,
            market_slug=snapshot.market_slug,
            sports_market_type=descriptor.sports_market_type,
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
    if descriptor.market_family != SportsMarketFamily.SINGLE_GAME:
        return ""
    return ""


def _is_game_ended(game: LiveGameState) -> bool:
    """比赛已结束的综合判断。

    status=ENDED 或 period 文本表示终局均视为结束。
    源冲突解析可能让 status="live" 优先，但 period="Finished" 同样意味着比赛结束。
    """
    if game.status == LiveGameStatus.ENDED:
        return True
    period_lower = (game.period or "").strip().lower()
    return period_lower in ("finished", "ft", "final", "full time", "end", "ap", "aet")


def _min_game_duration_seconds(league: str | None) -> int:
    """返回一场正式比赛所需的最短时间（秒），用于过滤不可能的 ENDED 状态。

    任何运动完成一场正式比赛都需要至少 60 分钟；棒球/篮球/足球通常 ≥ 90 分钟。
    宁可偏保守（避免误买）——真正在末局的比赛 elapsed_seconds 一定远超这些阈值。
    """
    text = (league or "").lower()
    if any(k in text for k in ("baseball", "mlb", "kbo", "npb")):
        return 5400  # 90 分钟；MLB 实际平均超过 3 小时
    if any(k in text for k in ("basketball", "nba", "nbl", "wnba")):
        return 5400
    if any(k in text for k in ("hockey", "nhl")):
        return 5400
    if any(k in text for k in ("football", "nfl", "ncaa")):
        return 5400
    if any(k in text for k in ("soccer", "football")):
        return 5400
    if "tennis" in text:
        return 3600  # 网球最短约 60 分钟
    return 3600  # 保守兜底


def _has_open_order(snapshot: AllocationMarketSnapshot, side: OrderSide) -> bool:
    """判断当前 token 是否已有开放订单，避免入场计划重复占仓。"""

    return any(order.side == side and order.open for order in snapshot.open_orders)


def _goalserve_odds_metadata(context: ExtensionContext) -> dict[str, object]:
    """从 context.metadata 抽出 Goalserve 盘口赔率，透传给 SportsMarketSnapshot。

    赔率差价评估器（odds_gap.py）需要 ``goalserve_moneyline`` 的去抽水原料，但
    evaluator 只接收 SportsMarketSnapshot；通过 snapshot.metadata 这一既有通道把
    赔率带进纯领域评估器，避免给评估器加 Goalserve 专属参数。
    """

    odds: dict[str, object] = {}
    for key in ("goalserve_moneyline", "goalserve_totals", "goalserve_spread"):
        value = context.metadata.get(key)
        if isinstance(value, dict):
            odds[key] = value
    return odds


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


def _goalserve_strong_edge_signal(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    target_side: SportsMarketSide,
) -> dict[str, object]:
    """当 Goalserve 对目标方向的隐含概率显著高于 Polymarket ask 时，返回强确认 metadata。

    - `goalserve_edge_confirmed=True` 时说明 Goalserve 强力背书我方仓位，
      price_cap 可按 `goalserve_strong_edge_price_bonus` 加成。
    - 总是返回 dict（可能为空），供调用侧合并进 metadata。
    """
    threshold = config.goalserve_strong_edge_threshold
    bonus = config.goalserve_strong_edge_price_bonus
    if threshold <= 0:
        return {}
    gs = context.metadata.get("goalserve_moneyline")
    if gs is None:
        gs = context.metadata.get("pregame_moneyline")
    if not isinstance(gs, dict) or gs.get("suspended"):
        return {}
    if target_side == SportsMarketSide.HOME:
        gs_implied = gs.get("home_implied_prob")
        suspended = gs.get("home_suspended")
    elif target_side == SportsMarketSide.AWAY:
        gs_implied = gs.get("away_implied_prob")
        suspended = gs.get("away_suspended")
    else:
        return {}
    if suspended or gs_implied is None:
        return {}
    try:
        gs_prob = float(gs_implied)
    except (TypeError, ValueError):
        return {}
    orderbook = context.orderbook
    if orderbook is None:
        return {}
    poly_ask = float(orderbook.best_ask or 0)
    if poly_ask <= 0:
        return {}
    edge = gs_prob - poly_ask
    if edge < float(threshold):
        return {
            "goalserve_edge_confirmed": False,
            "goalserve_edge_magnitude": round(edge, 4),
        }
    return {
        "goalserve_edge_confirmed": True,
        "goalserve_edge_magnitude": round(edge, 4),
        "goalserve_edge_gs_implied_prob": gs_prob,
        "goalserve_edge_poly_ask": poly_ask,
        "goalserve_edge_price_bonus": float(bonus) if config.goalserve_strong_edge_enabled else 0.0,
    }


def _goalserve_spread_conflict_signal(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    target_side: SportsMarketSide,
) -> dict[str, object]:
    """检测 Goalserve Spread 盘口方向与目标方向是否冲突。

    Spread 盘口可揭示书商对"谁赢多少分"的判断；若 Spread 偏向对手方而 Moneyline 倾向我方，
    两信号冲突时写入警告 metadata，供 calibration 分析是否调整入场策略。
    当前不直接拒绝入场——仅审计。
    """
    threshold = config.goalserve_spread_conflict_threshold
    gs_spread = context.metadata.get("goalserve_spread")
    if not isinstance(gs_spread, dict) or gs_spread.get("suspended"):
        return {}
    if target_side == SportsMarketSide.HOME:
        our_implied = gs_spread.get("home_implied_prob")
        opp_implied = gs_spread.get("away_implied_prob")
    elif target_side == SportsMarketSide.AWAY:
        our_implied = gs_spread.get("away_implied_prob")
        opp_implied = gs_spread.get("home_implied_prob")
    else:
        return {}
    if our_implied is None or opp_implied is None:
        return {}
    try:
        our_prob = float(our_implied)
        opp_prob = float(opp_implied)
    except (TypeError, ValueError):
        return {}
    conflict = opp_prob - our_prob
    if conflict <= 0:
        return {
            "goalserve_spread_conflict": False,
            "goalserve_spread_our_prob": our_prob,
        }
    return {
        "goalserve_spread_conflict": conflict > float(threshold),
        "goalserve_spread_conflict_magnitude": round(conflict, 4),
        "goalserve_spread_our_prob": our_prob,
        "goalserve_spread_opp_prob": opp_prob,
    }


def _goalserve_halftime_veto(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    target_side: SportsMarketSide,
) -> dict[str, object] | None:
    """若 Goalserve 半场 Money Line 对目标方向隐含概率低于阈值，返回拒绝 metadata。

    半场盘口反映书商对剩余比赛时段的判断：若目标方在半场盘口的赢率仍然很低，
    说明当前领先优势不足以支撑剩余时间，不应入场。
    返回 None 表示无否决（数据不足或目标方半场胜率足够）。
    """
    if not config.goalserve_halftime_veto_enabled:
        return None
    min_implied = config.goalserve_halftime_veto_min_implied
    if min_implied <= 0:
        return None
    ht = context.metadata.get("goalserve_halftime")
    if not isinstance(ht, dict) or ht.get("suspended"):
        return None
    if target_side == SportsMarketSide.HOME:
        ht_implied = ht.get("home_implied_prob")
        if ht.get("home_suspended"):
            return None
    elif target_side == SportsMarketSide.AWAY:
        ht_implied = ht.get("away_implied_prob")
        if ht.get("away_suspended"):
            return None
    else:
        return None
    if ht_implied is None:
        return None
    try:
        ht_prob = float(ht_implied)
    except (TypeError, ValueError):
        return None
    if ht_prob >= float(min_implied):
        return None
    # 半场赔率显示目标方胜率低于阈值——否决入场
    return {
        "goalserve_halftime_veto_target_side": target_side.value,
        "goalserve_halftime_veto_ht_implied_prob": ht_prob,
        "goalserve_halftime_veto_threshold": float(min_implied),
        "goalserve_halftime_market_name": ht.get("market_name"),
    }


def _sport_base_moneyline_lead(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> int:
    """返回当前运动对应的 min_moneyline_lead 基准值。

    足球和冰球是低分运动，用各自专属阈值；其余用全局值。
    """
    from strategies.sports_framework import is_hockey_game, is_soccer_game
    from strategies.sports_framework.parsing import live_game_state_from_metadata

    game = live_game_state_from_metadata(context.metadata)
    if game is not None:
        if is_soccer_game(game):
            return config.tail_soccer_min_moneyline_lead
        if is_hockey_game(game):
            return config.tail_hockey_min_moneyline_lead
    return config.tail_min_moneyline_lead


def _goalserve_policy_adjustments(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    target_side: SportsMarketSide,
) -> dict[str, object]:
    """根据 Goalserve 多信号协同计算 TailPolicy 调参字典，供 dataclasses.replace() 使用。

    调参逻辑：
    - Spread 冲突（对手方 spread 隐含概率 > 我方 + threshold）→ 额外要求更大领先优势
    - Spread + Moneyline 均确认我方 → 放宽领先要求 + 扩展买入时窗
    - Halftime ML 确认我方（即使未达否决阈值，仍有信号价值）→ 进一步扩展时窗

    返回空字典表示不做任何策略调整。
    """
    if not config.goalserve_policy_adjustment_enabled:
        return {}

    gs_ml = context.metadata.get("goalserve_moneyline")
    if gs_ml is None:
        gs_ml = context.metadata.get("pregame_moneyline")
    gs_spread = context.metadata.get("goalserve_spread")
    gs_ht = context.metadata.get("goalserve_halftime")

    # 判断 Moneyline 是否确认我方
    ml_confirmed = False
    if isinstance(gs_ml, dict) and not gs_ml.get("suspended"):
        if target_side == SportsMarketSide.HOME:
            ml_implied = gs_ml.get("home_implied_prob")
            ml_susp = gs_ml.get("home_suspended")
        elif target_side == SportsMarketSide.AWAY:
            ml_implied = gs_ml.get("away_implied_prob")
            ml_susp = gs_ml.get("away_suspended")
        else:
            ml_implied, ml_susp = None, False
        if not ml_susp and ml_implied is not None:
            try:
                ml_confirmed = float(ml_implied) >= 0.5
            except (TypeError, ValueError):
                pass

    # 判断 Spread 是否确认我方（我方 spread 隐含概率 > 对手）
    spread_confirmed = False
    spread_conflict = False
    if isinstance(gs_spread, dict) and not gs_spread.get("suspended"):
        if target_side == SportsMarketSide.HOME:
            our_sp = gs_spread.get("home_implied_prob")
            opp_sp = gs_spread.get("away_implied_prob")
        elif target_side == SportsMarketSide.AWAY:
            our_sp = gs_spread.get("away_implied_prob")
            opp_sp = gs_spread.get("home_implied_prob")
        else:
            our_sp, opp_sp = None, None
        if our_sp is not None and opp_sp is not None:
            try:
                conflict_margin = float(opp_sp) - float(our_sp)
                if conflict_margin > float(config.goalserve_spread_conflict_threshold):
                    spread_conflict = True
                elif float(our_sp) > float(opp_sp):
                    spread_confirmed = True
            except (TypeError, ValueError):
                pass

    # 判断 Halftime ML 是否确认我方
    ht_confirmed = False
    if isinstance(gs_ht, dict) and not gs_ht.get("suspended"):
        if target_side == SportsMarketSide.HOME:
            ht_implied = gs_ht.get("home_implied_prob")
            ht_susp = gs_ht.get("home_suspended")
        elif target_side == SportsMarketSide.AWAY:
            ht_implied = gs_ht.get("away_implied_prob")
            ht_susp = gs_ht.get("away_suspended")
        else:
            ht_implied, ht_susp = None, False
        if not ht_susp and ht_implied is not None:
            try:
                ht_confirmed = float(ht_implied) >= 0.5
            except (TypeError, ValueError):
                pass

    adjustments: dict[str, object] = {}
    base_lead = _sport_base_moneyline_lead(config, context)

    if spread_conflict:
        # Spread 冲突：要求更大领先优势以抵消不确定性
        extra = config.goalserve_spread_conflict_extra_lead
        adjustments["min_moneyline_lead"] = base_lead + extra

    elif ml_confirmed and spread_confirmed:
        # 多信号确认：放宽领先要求，允许更早入场
        relief = config.goalserve_multi_confirm_lead_relief
        adjustments["min_moneyline_lead"] = max(1, base_lead - relief)
        time_bonus = config.goalserve_multi_confirm_time_bonus_seconds
        adjustments["max_moneyline_seconds_remaining"] = (
            config.tail_max_moneyline_seconds_remaining + time_bonus
        )
        if ht_confirmed:
            # 半场也确认：进一步扩展时窗
            adjustments["max_moneyline_seconds_remaining"] = (
                config.tail_max_moneyline_seconds_remaining + time_bonus * 2
            )

    return adjustments


def _goalserve_moneyline_veto(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    target_side: SportsMarketSide,
) -> dict[str, object] | None:
    """若 Goalserve 赔率对目标方向隐含概率显著低于 Polymarket ask，返回拒绝 metadata。

    返回 None 表示无否决（交叉验证通过或数据不足无法判断）。
    只有在 config.goalserve_cross_validation_enabled=True 且
    config.goalserve_cross_validation_margin > 0 时才产生否决。
    """
    if not config.goalserve_cross_validation_enabled:
        return None
    margin = config.goalserve_cross_validation_margin
    if margin <= 0:
        return None
    # 只用 inplay 赔率做否决：赛前赔率已不反映当前比赛进程（比赛尾段 Polymarket ask
    # 大幅高于赛前赔率是正常的），用 pregame 做否决会产生大量假阳性拒绝。
    # inplay 赔率不可用（None 或暂停）时放行，不否决。
    gs = context.metadata.get("goalserve_moneyline")
    if not isinstance(gs, dict):
        return None
    if gs.get("suspended"):
        return None
    # 取目标方向隐含概率
    if target_side == SportsMarketSide.HOME:
        gs_implied = gs.get("home_implied_prob")
        if gs.get("home_suspended"):
            return None
    elif target_side == SportsMarketSide.AWAY:
        gs_implied = gs.get("away_implied_prob")
        if gs.get("away_suspended"):
            return None
    else:
        return None
    if gs_implied is None:
        return None
    try:
        gs_prob = float(gs_implied)
    except (TypeError, ValueError):
        return None
    orderbook = context.orderbook
    if orderbook is None:
        return None
    poly_ask = float(orderbook.best_ask or 0)
    if poly_ask <= 0:
        return None
    discrepancy = poly_ask - gs_prob
    if discrepancy <= float(margin):
        return None
    # 否决：Goalserve 认为目标方概率比 Polymarket ask 低超过阈值
    return {
        "goalserve_veto_target_side": target_side.value,
        "goalserve_veto_gs_implied_prob": gs_prob,
        "goalserve_veto_poly_ask": poly_ask,
        "goalserve_veto_discrepancy": round(discrepancy, 4),
        "goalserve_veto_threshold": float(margin),
        "goalserve_market_name": gs.get("market_name"),
    }
