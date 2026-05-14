"""Outright family 入场决策。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.extension_api import DecisionKind, ExtensionContext, ExtensionDecision, ExtensionPorts, MarketTokenView

from strategies.current.config import CurrentStrategyConfig
from strategies.current.outright.evaluator import evaluate_outright_opportunity
from strategies.current.outright.match import season_odds_from_metadata
from strategies.current.outright.risk import check_outright_entry_risk
from strategies.current.outright.types import OutrightEvaluation
from strategies.current.parameter_overrides import effective_decimal, effective_int
from strategies.current.tail.types import ExecutionPermission
from strategies.current.trading.helpers import decimal_from_metadata
from strategies.current.trading.helpers import bid_plus_tick_fallback_ask, bid_plus_tick_fallback_metadata


@dataclass(frozen=True, slots=True)
class _MockTokenView:
    """测试兜底 token view（生产路径 framework 注入真正的 MarketTokenView）。"""

    token_id: str
    outcome: str
    orderbook: OrderbookSnapshot | None = None


def decide_outright_entry(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    ports: ExtensionPorts | None = None,
) -> ExtensionDecision:
    """遍历每个 outcome token，对每个 token 跑评估器；选 edge 最大的接受候选。"""
    market = context.market
    if market is None:
        return ExtensionDecision.skip(reason="outright_missing_market")
    snapshot = season_odds_from_metadata(context.metadata or {})
    token_views = tuple(context.market_token_views or ())
    outcomes = market.outcomes
    if not token_views and not outcomes:
        return ExtensionDecision.skip(
            reason="outright_missing_outcomes",
            metadata={"market_family": "outright"},
        )
    if not token_views:
        token_views = tuple(
            _MockTokenView(token_id=o.token_id, outcome=o.outcome)
            for o in outcomes
        )
    permission = config.tail_outright_execution_permission
    budget_usdc = effective_decimal(ports, "tail_outright_budget_usdc", config.tail_outright_budget_usdc)
    budget_unlocked = budget_usdc > Decimal("0")
    effective_permission = permission if budget_unlocked else ExecutionPermission.RECORD_ONLY
    now = context.now or datetime.now(timezone.utc)
    _TokenView = _MockTokenView | MarketTokenView
    best_accept: tuple[tuple[Decimal, OutrightEvaluation], _TokenView] | None = None
    first_reject: tuple[OutrightEvaluation, _TokenView] | None = None
    for token_view in token_views:
        orderbook = token_view.orderbook
        best_ask = orderbook.best_ask if orderbook is not None else None
        permission_for_token = effective_permission
        fallback_meta: dict[str, str] = {}
        if best_ask is None and orderbook is not None:
            fallback_ask = bid_plus_tick_fallback_ask(orderbook, market.tick_size)
            if fallback_ask is not None:
                best_ask = fallback_ask
                permission_for_token = ExecutionPermission.RECORD_ONLY
                fallback_meta = bid_plus_tick_fallback_metadata(
                    orderbook=orderbook,
                    tick_size=market.tick_size,
                    fallback_ask=fallback_ask,
                )
        buyable = (
            orderbook.buyable_ask_depth(max_price=config.tail_outright_max_entry_price)
            if orderbook is not None
            else Decimal("0")
        )
        buyable_usdc = buyable * best_ask if best_ask is not None else Decimal("0")
        evaluation = evaluate_outright_opportunity(
            snapshot=snapshot,
            market=market,
            outcome_label=token_view.outcome,
            token_id=token_view.token_id,
            best_ask=best_ask,
            best_bid=orderbook.best_bid if orderbook is not None else None,
            buyable_liquidity_usdc=buyable_usdc,
            now=now,
            max_season_odds_age_seconds=config.tail_outright_max_season_odds_age_seconds,
            min_edge_bps=effective_int(ports, "tail_outright_min_edge_bps", config.tail_outright_min_edge_bps),
            max_entry_price=effective_decimal(ports, "tail_outright_max_entry_price", config.tail_outright_max_entry_price),
            min_orderbook_depth_usdc=effective_decimal(ports, "tail_outright_min_orderbook_depth_usdc", config.tail_outright_min_orderbook_depth_usdc),
            exit_edge_target=effective_decimal(ports, "tail_outright_exit_edge_target", config.tail_outright_exit_edge_target),
            min_profit_per_share=effective_decimal(ports, "tail_outright_min_profit_per_share", config.tail_outright_min_profit_per_share),
            execution_permission=permission_for_token,
            extra_metadata=fallback_meta or None,
        )
        if evaluation.accepted:
            edge = (evaluation.fair_value or Decimal(0)) - (best_ask or Decimal(0))
            if best_accept is None or edge > best_accept[0][0]:
                best_accept = ((edge, evaluation), token_view)
        elif first_reject is None:
            first_reject = (evaluation, token_view)

    if best_accept is None:
        evaluation, token_view = first_reject if first_reject else (None, None)
        if evaluation is None:
            return ExtensionDecision.skip(
                reason="outright_no_candidates",
                metadata={"market_family": "outright"},
            )
        return ExtensionDecision.skip(
            reason=evaluation.reason,
            metadata={
                "market_family": "outright",
                "outright_action": evaluation.action.value,
                "outright_reject_reason": (
                    evaluation.reject_reason.value if evaluation.reject_reason else None
                ),
                "outright_accepted": False,
                "outright_metadata": dict(evaluation.metadata),
            },
        )
    (_edge, evaluation), token_view = best_accept
    if evaluation.action.value != "auto_execute":
        return ExtensionDecision.skip(
            reason=f"outright_{evaluation.action.value}",
            metadata={
                "market_family": "outright",
                "outright_action": evaluation.action.value,
                "outright_accepted": True,
                "outright_metadata": dict(evaluation.metadata),
                "budget_unlocked": budget_unlocked,
            },
        )
    allocation = context.allocation
    if allocation is not None and allocation.buy_budget_usdc > Decimal("0"):
        proposed_amount = allocation.buy_budget_usdc
    else:
        proposed_amount = min(budget_usdc, config.tail_outright_max_per_market_usdc)
    if proposed_amount <= Decimal("0"):
        return ExtensionDecision.skip(
            reason="outright_budget_exhausted",
            metadata={"market_family": "outright"},
        )
    market_end_at = market.end_date
    ctx_meta = context.metadata or {}
    existing_outright_exposure = decimal_from_metadata(ctx_meta.get("outright_total_exposure_usdc")) or Decimal("0")
    existing_event_exposure = decimal_from_metadata(ctx_meta.get("outright_event_exposure_usdc")) or Decimal("0")
    risk_reject = check_outright_entry_risk(
        now=now,
        market_end_at=market_end_at,
        proposed_amount_usdc=proposed_amount,
        existing_outright_exposure_usdc=existing_outright_exposure,
        existing_event_exposure_usdc=existing_event_exposure,
        max_per_market_usdc=config.tail_outright_max_per_market_usdc,
        max_event_correlation_usdc=config.tail_outright_max_event_correlation_usdc,
        max_total_outright_usdc=budget_usdc,
        max_hold_horizon_days=config.tail_outright_max_hold_horizon_days,
        min_remaining_days=config.tail_outright_min_remaining_days,
    )
    if risk_reject is not None:
        return ExtensionDecision.skip(
            reason=f"outright_{risk_reject.value}",
            metadata={
                "market_family": "outright",
                "outright_reject_reason": risk_reject.value,
                "outright_metadata": dict(evaluation.metadata),
            },
        )
    return ExtensionDecision.buy(
        reason=evaluation.reason,
        token_id=evaluation.candidate.token_id if evaluation.candidate else None,
        price=evaluation.entry_price_cap,  # evaluator asserts non-None when accepted
        amount_usdc=proposed_amount,
        market_slug=market.market_slug,
        decision_kind=DecisionKind.ENTRY,
        metadata={
            "market_family": "outright",
            "outright_metadata": dict(evaluation.metadata),
            "outright_fair_value": str(evaluation.fair_value) if evaluation.fair_value else None,
            "outright_exit_price_target": (
                str(evaluation.exit_price_target) if evaluation.exit_price_target else None
            ),
        },
    )


def resolve_outright_reject_label(decision: ExtensionDecision) -> str:
    metadata = decision.metadata or {}
    reason = metadata.get("outright_reject_reason")
    if isinstance(reason, str) and reason:
        return reason
    if decision.reason:
        return decision.reason
    return "unspecified"
