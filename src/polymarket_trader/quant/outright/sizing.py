"""Outright family Kelly sizing。"""

from __future__ import annotations

import logging
from decimal import Decimal

from polymarket_trader.domain.allocation import AllocationPlan
from polymarket_trader.domain.decisions import DecisionContext, EntrySizing

from polymarket_trader.runtime.runtime_ports import RuntimePorts

from polymarket_trader.quant.allocation import AllocationMarketSnapshot, ProbView, kelly_plan
from polymarket_trader.quant.config import TradingWorkflowConfig
from polymarket_trader.quant.outright.match import season_odds_from_metadata
from polymarket_trader.quant.outright.pricing import outright_fair_value
from polymarket_trader.quant.parameter_overrides import effective_decimal

logger = logging.getLogger(__name__)


def size_outright_entry(
    config: TradingWorkflowConfig,
    context: DecisionContext,
    ports: RuntimePorts | None = None,
) -> EntrySizing:
    """用赛季赔率作为真概率（conf=1.0）喂 Kelly 公式；独立预算包络，不占 single_game 资金。"""
    budget = effective_decimal(ports, "tail_outright_budget_usdc", config.tail_outright_budget_usdc)
    if budget <= Decimal("0"):
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=budget,
                reason="outright_budget_zero",
            ),
            reason="outright_budget_zero",
        )

    market = context.market
    if market is None:
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=budget,
                reason="outright_missing_market",
            ),
            reason="outright_missing_market",
        )

    kelly_fraction = context.kelly_fraction
    kelly_max_position_fraction = context.kelly_max_position_fraction
    kelly_min_stake_usdc = context.kelly_min_stake_usdc
    if kelly_fraction is None or kelly_max_position_fraction is None or kelly_min_stake_usdc is None:
        logger.warning(
            "outright_sizing.missing_kelly_params: trace_id=%s condition_id=%s",
            context.trace_id,
            market.condition_id,
        )
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=budget,
                reason="outright_sizing_no_kelly_params",
            ),
            reason="outright_sizing_no_kelly_params",
            metadata={
                "outright_budget_usdc": str(budget),
                "kelly_path": "not_applied",
            },
        )

    snapshot = season_odds_from_metadata(context.metadata or {})
    token_views = tuple(context.market_token_views or ())
    outcome_by_token: dict[str, str] = {
        tv.token_id: (tv.outcome or "") for tv in token_views if tv.token_id
    }

    per_market_cap = min(budget, config.tail_outright_max_per_market_usdc)
    market_snapshots: list[AllocationMarketSnapshot] = []
    for tv in token_views:
        if not tv.token_id:
            continue
        ob = tv.orderbook
        best_ask = ob.best_ask if ob is not None else None
        market_snapshots.append(AllocationMarketSnapshot(
            market=market,
            token_id=tv.token_id,
            orderbook=ob,
            best_ask=best_ask,
            strategy_budget_cap_usdc=per_market_cap,
        ))

    def _prob_provider(snap: AllocationMarketSnapshot) -> ProbView:
        outcome_label = outcome_by_token.get(snap.token_id, "")
        if snapshot is None or not outcome_label:
            return ProbView(prob_p=None, prob_confidence=Decimal("0"), source="outright_real_missing")
        pricing_result = outright_fair_value(snapshot, snap.market, outcome_label)
        if pricing_result.value is None:
            source = (
                f"outright_real_rejected:{pricing_result.reject.value}"
                if pricing_result.reject is not None
                else "outright_real_missing"
            )
            return ProbView(prob_p=None, prob_confidence=Decimal("0"), source=source)
        return ProbView(prob_p=pricing_result.value, prob_confidence=Decimal("1"), source="outright_real")

    plan = kelly_plan(
        trace_id=context.trace_id,
        bankroll_usdc=context.bankroll_usdc or budget,
        portfolio_budget_usdc=budget,
        markets=tuple(market_snapshots),
        prob_provider=_prob_provider,
        kelly_fraction=kelly_fraction,
        kelly_max_position_fraction=kelly_max_position_fraction,
        kelly_min_edge=context.kelly_min_edge or Decimal("0"),
        kelly_min_stake_usdc=kelly_min_stake_usdc,
        kelly_allow_round_up_to_market_min=context.kelly_allow_round_up_to_market_min if context.kelly_allow_round_up_to_market_min is not None else True,
        kelly_round_up_max_overbet_ratio=context.kelly_round_up_max_overbet_ratio or Decimal("1"),
    )
    return EntrySizing(allocation_plan=plan, reason="outright_kelly")
