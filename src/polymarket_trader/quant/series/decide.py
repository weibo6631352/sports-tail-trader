"""Series family 入场决策（WINNER / TOTAL_GAMES / GAME_HANDICAP）。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.extension_api import DecisionKind, ExtensionContext, ExtensionDecision, ExtensionPorts, MarketTokenView

from polymarket_trader.quant.config import CurrentStrategyConfig
from polymarket_trader.quant.series.evaluator import SeriesEvaluatorInputs, evaluate_series_opportunity
from polymarket_trader.quant.series.risk import SeriesSubTypeRiskConfig, check_series_entry_risk
from polymarket_trader.quant.series.sizing import series_subtype_settings
from polymarket_trader.quant.series.types import SeriesCandidate, SeriesEvaluation
from polymarket_trader.quant.trading.helpers import decimal_from_metadata


def decide_series_entry(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    ports: ExtensionPorts | None = None,
) -> ExtensionDecision:
    """按 sub_type 选取风控 + 定价配置；accepted 路径走 Kelly 分配 + risk 前置 → BUY。"""
    market = context.market
    if market is None:
        return ExtensionDecision.skip(reason="series_missing_market")
    token_views = tuple(context.market_token_views or ())
    outcomes = market.outcomes
    if not token_views and not outcomes:
        return ExtensionDecision.skip(
            reason="series_missing_outcomes",
            metadata={"market_family": "series"},
        )
    if not token_views:
        token_views = tuple(
            MarketTokenView(token_id=o.token_id, outcome=o.outcome)
            for o in outcomes
        )

    sub_type, settings = series_subtype_settings(market, config, ports=ports)
    permission = settings.execution_permission
    budget_unlocked = settings.budget_usdc > Decimal("0")
    auto_enabled = budget_unlocked and permission.value == "auto_execute"
    now = context.now or datetime.now(timezone.utc)
    ctx_meta = context.metadata or {}

    _season_port = ports.season_state if ports is not None else None
    _season_snapshot = _season_port.season_snapshot() if _season_port is not None else None
    best_accept: tuple[tuple[Decimal, SeriesEvaluation], MarketTokenView] | None = None
    first_reject: tuple[SeriesEvaluation, MarketTokenView] | None = None
    for token_view in token_views:
        orderbook = token_view.orderbook
        best_ask = orderbook.best_ask if orderbook is not None else None
        buyable = (
            orderbook.buyable_ask_depth(max_price=settings.max_entry_price)
            if orderbook is not None
            else Decimal("0")
        )
        buyable_usdc = buyable * best_ask if best_ask is not None else Decimal("0")
        inputs = SeriesEvaluatorInputs(
            best_ask=best_ask,
            best_bid=orderbook.best_bid if orderbook is not None else None,
            buyable_liquidity_usdc=buyable_usdc,
            now=now,
            min_edge_bps=settings.min_edge_bps,
            max_entry_price=settings.max_entry_price,
            min_orderbook_depth_usdc=settings.min_orderbook_depth_usdc,
            max_series_state_age_seconds=settings.max_state_age_seconds,
            max_game_odds_age_seconds=settings.max_game_odds_age_seconds,
            season_snapshot=_season_snapshot,
        )
        candidate = SeriesCandidate(
            market=market,
            outcome_label=token_view.outcome,
            token_id=token_view.token_id,
            metadata=ctx_meta,
        )
        evaluation = evaluate_series_opportunity(candidate, inputs=inputs)
        if evaluation.accepted:
            edge = (evaluation.fair_value or Decimal(0)) - (best_ask or Decimal(0))
            if best_accept is None or edge > best_accept[0][0]:
                best_accept = ((edge, evaluation), token_view)
        elif first_reject is None:
            first_reject = (evaluation, token_view)

    if best_accept is None:
        evaluation, token_view = first_reject if first_reject else (None, None)
        if evaluation is None or token_view is None:
            return ExtensionDecision.skip(
                reason="series_no_candidates",
                metadata={"market_family": "series", "series_sub_type": sub_type.value},
            )
        return ExtensionDecision.skip(
            reason=evaluation.reject_reason.value if evaluation.reject_reason else "series_unclassified",
            metadata={
                "market_family": "series",
                "series_sub_type": evaluation.sub_type.value,
                "series_reject_reason": (
                    evaluation.reject_reason.value if evaluation.reject_reason else None
                ),
                "series_accepted": False,
                "series_metadata": dict(evaluation.metadata),
                "token_id": token_view.token_id,
                "outcome_label": token_view.outcome,
            },
        )

    (_edge, evaluation), token_view = best_accept
    if not auto_enabled:
        return ExtensionDecision.skip(
            reason=f"series_record_only_{permission.value}",
            metadata={
                "market_family": "series",
                "series_sub_type": evaluation.sub_type.value,
                "series_accepted": True,
                "series_metadata": dict(evaluation.metadata),
                "budget_unlocked": budget_unlocked,
                "execution_permission": permission.value,
                "token_id": token_view.token_id,
                "outcome_label": token_view.outcome,
                "tail_action": permission.value,
                "tail_reason": f"series_{evaluation.sub_type.value}_entry_accepted",
            },
        )

    allocation = context.allocation
    if allocation is not None and allocation.buy_budget_usdc > Decimal("0"):
        proposed_amount = allocation.buy_budget_usdc
    else:
        proposed_amount = min(settings.budget_usdc, settings.max_per_market_usdc)
    if proposed_amount <= Decimal("0"):
        return ExtensionDecision.skip(
            reason="series_budget_exhausted",
            metadata={"market_family": "series", "series_sub_type": sub_type.value},
        )

    existing_series_exposure = (
        decimal_from_metadata(ctx_meta.get("series_total_exposure_usdc")) or Decimal("0")
    )
    existing_event_exposure = (
        decimal_from_metadata(ctx_meta.get("series_event_exposure_usdc")) or Decimal("0")
    )
    risk_reject = check_series_entry_risk(
        now=now,
        market_end_at=market.end_date,
        proposed_amount_usdc=proposed_amount,
        existing_series_exposure_usdc=existing_series_exposure,
        existing_event_exposure_usdc=existing_event_exposure,
        config=SeriesSubTypeRiskConfig(
            max_per_market_usdc=settings.max_per_market_usdc,
            max_event_correlation_usdc=settings.max_event_correlation_usdc,
            max_total_series_usdc=settings.budget_usdc,
            max_hold_horizon_days=settings.max_hold_horizon_days,
            min_remaining_days=settings.min_remaining_days,
        ),
    )
    if risk_reject is not None:
        return ExtensionDecision.skip(
            reason=f"series_{risk_reject.value}",
            metadata={
                "market_family": "series",
                "series_sub_type": sub_type.value,
                "series_reject_reason": risk_reject.value,
                "series_metadata": dict(evaluation.metadata),
            },
        )

    # 用 best_ask 作为 FAK 订单价格，与 single_game tail 行为一致（hooks.py）。
    # entry_price_cap 已在 evaluator 验证 best_ask <= cap，不用于实际下单限价。
    # 这样 risk manager 计算 amount_usdc / price 得到的 shares 等于实际成交量，
    # 避免因 cap > best_ask 导致 min_order_not_met 误拒。
    order_best_ask = token_view.orderbook.best_ask if token_view.orderbook is not None else None
    if order_best_ask is None:
        return ExtensionDecision.skip(
            reason="series_missing_best_ask_for_order",
            metadata={"market_family": "series", "series_sub_type": sub_type.value},
        )

    # 资金效率门禁：用系列赛热态估算实际结算天数，排除长周期低效订单。
    # 封盘时间 ≠ 结算时间：某些市场在单场结束后数天才结算（等待系列完结）。
    settlement_days = _series_estimated_settlement_days(
        ctx_meta,
        dict(evaluation.metadata),
        now=now,
        avg_days_per_game=config.tail_series_avg_days_per_game,
        settlement_buffer_minutes=config.tail_settlement_buffer_minutes,
    )
    eff_passed, eff_meta = _series_capital_efficiency_metadata(
        proposed_amount,
        order_best_ask,
        settlement_days,
        config.tail_series_min_expected_profit_per_day_usdc,
    )
    if not eff_passed:
        return ExtensionDecision.skip(
            reason="series_capital_efficiency_below_min",
            metadata={
                "market_family": "series",
                "series_sub_type": sub_type.value,
                "series_metadata": dict(evaluation.metadata),
                **eff_meta,
            },
        )

    entry_price_cap = _series_entry_price(evaluation, fallback=settings.max_entry_price)
    return ExtensionDecision.buy(
        reason=f"series_{sub_type.value}_entry_accepted",
        token_id=evaluation.candidate.token_id,
        price=order_best_ask,
        amount_usdc=proposed_amount,
        market_slug=market.market_slug,
        decision_kind=DecisionKind.ENTRY,
        metadata={
            "market_family": "series",
            "series_sub_type": evaluation.sub_type.value,
            "series_metadata": dict(evaluation.metadata),
            "series_fair_value": str(evaluation.fair_value) if evaluation.fair_value else None,
            "series_entry_price_cap": str(entry_price_cap),
            "tail_action": "auto_execute",
            "tail_reason": f"series_{sub_type.value}_entry_accepted",
            "execution_permission": permission.value,
            **eff_meta,
        },
    )


def _series_estimated_settlement_days(
    ctx_meta: Mapping[str, Any],
    evaluation_metadata: Mapping[str, Any],
    *,
    now: datetime,
    avg_days_per_game: float,
    settlement_buffer_minutes: int,
) -> float:
    """从系列赛热态估算资金占用天数。

    优先用 expected_games_remaining 计算（基于当前比分和单场胜率）；
    next_game_at 已知时精确计算到首场的等待时间；否则按 avg_days_per_game 估算。
    仅 WINNER 子类型的 metadata 包含 p_per_game；TOTAL_GAMES / HANDICAP 缺失时
    用盈亏中性估计（needed_wins 平均值）作为保守回退，避免完全跳过效率检查。
    """
    from polymarket_trader.quant.series.match import series_state_from_metadata
    from polymarket_trader.quant.series.winner_model import expected_games_remaining

    state = series_state_from_metadata(ctx_meta)
    if state is None:
        return -1.0

    p_raw = evaluation_metadata.get("p_per_game")
    if p_raw is not None:
        try:
            p_per_game = Decimal(str(p_raw))
        except Exception:
            p_per_game = Decimal("0.5")
    else:
        # 没有 p_per_game 时（TOTAL_GAMES/HANDICAP）用 0.5 作为中性估计
        p_per_game = Decimal("0.5")

    exp_games = expected_games_remaining(state, p_per_game)
    buffer_days = settlement_buffer_minutes / 60.0 / 24.0

    next_game_at = state.next_game_at
    if next_game_at is not None:
        tz_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        tz_next = next_game_at if next_game_at.tzinfo else next_game_at.replace(tzinfo=timezone.utc)
        if tz_next > tz_now:
            days_to_next = (tz_next - tz_now).total_seconds() / 86400.0
            remaining_after_first = max(0.0, exp_games - 1.0)
            return days_to_next + remaining_after_first * avg_days_per_game + buffer_days

    return exp_games * avg_days_per_game + buffer_days


def _series_capital_efficiency_metadata(
    proposed_amount: Decimal,
    entry_price: Decimal,
    settlement_days: float,
    min_per_day_usdc: Decimal,
) -> tuple[bool, dict[str, object]]:
    """计算 series 入场的资金效率并返回 (通过, metadata)。

    通过条件：每天预期利润 ≥ min_per_day_usdc。
    预期利润 = shares × (1 - entry_price)（假设结算时 token 价值 = 1）。
    """
    if entry_price <= Decimal("0") or entry_price >= Decimal("1") or settlement_days <= 0:
        return True, {}
    shares = proposed_amount / entry_price
    expected_profit = shares * (Decimal("1") - entry_price)
    expected_profit_per_day = expected_profit / Decimal(str(settlement_days))
    meta: dict[str, object] = {
        "series_estimated_settlement_days": round(settlement_days, 2),
        "series_expected_profit_usdc": str(expected_profit.quantize(Decimal("0.0001"))),
        "series_expected_profit_per_day_usdc": str(expected_profit_per_day.quantize(Decimal("0.0001"))),
        "series_min_expected_profit_per_day_usdc": str(min_per_day_usdc),
    }
    passed = expected_profit_per_day >= min_per_day_usdc
    return passed, meta


def resolve_series_sub_type_label(decision: ExtensionDecision) -> str:
    metadata = decision.metadata or {}
    sub_type = metadata.get("series_sub_type")
    if isinstance(sub_type, str) and sub_type:
        return sub_type
    return "unknown"


def resolve_series_reject_label(decision: ExtensionDecision) -> str:
    metadata = decision.metadata or {}
    reason = metadata.get("series_reject_reason")
    if isinstance(reason, str) and reason:
        return reason
    if decision.reason:
        return decision.reason
    return "unspecified"


def _series_entry_price(evaluation: SeriesEvaluation, *, fallback: Decimal) -> Decimal:
    cap_text = evaluation.metadata.get("entry_price_cap") if evaluation.metadata else None
    cap = decimal_from_metadata(cap_text)
    if cap is not None and cap > Decimal("0"):
        return cap
    return fallback
