"""Series family Kelly sizing（WINNER / TOTAL_GAMES / GAME_HANDICAP 共享）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.allocation import AllocationPlan
from polymarket_trader.domain.market import Market
from polymarket_trader.extension_api import EntrySizing, ExtensionContext

from polymarket_trader.extension_api import ExtensionPorts

from polymarket_trader.quant.allocation import AllocationMarketSnapshot, ProbView, kelly_plan
from polymarket_trader.quant.config import CurrentStrategyConfig
from polymarket_trader.quant.parameter_overrides import effective_decimal, effective_int, effective_str_enum
from polymarket_trader.quant.series.classifier import classify_series_sub_type
from polymarket_trader.quant.series.evaluator import SeriesEvaluatorInputs, evaluate_series_opportunity
from polymarket_trader.quant.series.types import SeriesCandidate, SeriesSubType
from polymarket_trader.quant.tail.types import ExecutionPermission

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SeriesSubTypeSettings:
    """sub_type 维度的策略配置切片：解耦 sizing / decide 与 CurrentStrategyConfig。"""

    execution_permission: ExecutionPermission
    min_edge_bps: int
    max_entry_price: Decimal
    budget_usdc: Decimal
    max_per_market_usdc: Decimal
    max_event_correlation_usdc: Decimal
    min_orderbook_depth_usdc: Decimal
    max_state_age_seconds: int
    max_game_odds_age_seconds: int
    max_hold_horizon_days: int
    min_remaining_days: int


def series_subtype_settings(
    market: Market,
    config: CurrentStrategyConfig,
    ports: ExtensionPorts | None = None,
) -> tuple[SeriesSubType, SeriesSubTypeSettings]:
    """根据 market 的子类型分类返回对应配置包络。

    OTHER 子类型也走 winner 配置（evaluator 会直接 SUBTYPE_UNCLASSIFIED 拒绝）。
    ports 存在时，winner 子类型允许运行时 override（budget/edge/entry_price/depth）。
    """

    sub_type = classify_series_sub_type(market)
    if sub_type == SeriesSubType.TOTAL_GAMES:
        return sub_type, SeriesSubTypeSettings(
            execution_permission=effective_str_enum(
                ports, "tail_series_total_games_execution_permission",
                config.tail_series_total_games_execution_permission,
                ExecutionPermission,
            ),
            min_edge_bps=effective_int(ports, "tail_series_total_games_min_edge_bps", config.tail_series_total_games_min_edge_bps),
            max_entry_price=effective_decimal(ports, "tail_series_total_games_max_entry_price", config.tail_series_total_games_max_entry_price),
            budget_usdc=effective_decimal(ports, "tail_series_total_games_budget_usdc", config.tail_series_total_games_budget_usdc),
            max_per_market_usdc=config.tail_series_total_games_max_per_market_usdc,
            max_event_correlation_usdc=config.tail_series_total_games_max_event_correlation_usdc,
            min_orderbook_depth_usdc=effective_decimal(ports, "tail_series_total_games_min_orderbook_depth_usdc", config.tail_series_total_games_min_orderbook_depth_usdc),
            max_state_age_seconds=config.tail_series_winner_max_state_age_seconds,
            max_game_odds_age_seconds=config.tail_series_winner_max_game_odds_age_seconds,
            max_hold_horizon_days=config.tail_series_total_games_max_hold_horizon_days,
            min_remaining_days=config.tail_series_total_games_min_remaining_days,
        )
    if sub_type == SeriesSubType.GAME_HANDICAP:
        return sub_type, SeriesSubTypeSettings(
            execution_permission=effective_str_enum(
                ports, "tail_series_handicap_execution_permission",
                config.tail_series_handicap_execution_permission,
                ExecutionPermission,
            ),
            min_edge_bps=effective_int(ports, "tail_series_handicap_min_edge_bps", config.tail_series_handicap_min_edge_bps),
            max_entry_price=effective_decimal(ports, "tail_series_handicap_max_entry_price", config.tail_series_handicap_max_entry_price),
            budget_usdc=effective_decimal(ports, "tail_series_handicap_budget_usdc", config.tail_series_handicap_budget_usdc),
            max_per_market_usdc=config.tail_series_handicap_max_per_market_usdc,
            max_event_correlation_usdc=config.tail_series_handicap_max_event_correlation_usdc,
            min_orderbook_depth_usdc=effective_decimal(ports, "tail_series_handicap_min_orderbook_depth_usdc", config.tail_series_handicap_min_orderbook_depth_usdc),
            max_state_age_seconds=config.tail_series_winner_max_state_age_seconds,
            max_game_odds_age_seconds=config.tail_series_winner_max_game_odds_age_seconds,
            max_hold_horizon_days=config.tail_series_handicap_max_hold_horizon_days,
            min_remaining_days=config.tail_series_handicap_min_remaining_days,
        )
    # WINNER 与 OTHER 共用 winner 配置；OTHER 在 evaluator 立即 SUBTYPE_UNCLASSIFIED 拒绝。
    effective_sub_type = SeriesSubType.WINNER if sub_type == SeriesSubType.OTHER else sub_type
    return effective_sub_type, SeriesSubTypeSettings(
        execution_permission=effective_str_enum(
            ports, "tail_series_winner_execution_permission",
            config.tail_series_winner_execution_permission,
            ExecutionPermission,
        ),
        min_edge_bps=effective_int(ports, "tail_series_winner_min_edge_bps", config.tail_series_winner_min_edge_bps),
        max_entry_price=effective_decimal(ports, "tail_series_winner_max_entry_price", config.tail_series_winner_max_entry_price),
        budget_usdc=effective_decimal(ports, "tail_series_winner_budget_usdc", config.tail_series_winner_budget_usdc),
        max_per_market_usdc=config.tail_series_winner_max_per_market_usdc,
        max_event_correlation_usdc=config.tail_series_winner_max_event_correlation_usdc,
        min_orderbook_depth_usdc=effective_decimal(ports, "tail_series_winner_min_orderbook_depth_usdc", config.tail_series_winner_min_orderbook_depth_usdc),
        max_state_age_seconds=config.tail_series_winner_max_state_age_seconds,
        max_game_odds_age_seconds=config.tail_series_winner_max_game_odds_age_seconds,
        max_hold_horizon_days=config.tail_series_winner_max_hold_horizon_days,
        min_remaining_days=config.tail_series_winner_min_remaining_days,
    )


def size_series_entry(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    ports: ExtensionPorts | None = None,
) -> EntrySizing:
    """Series Kelly sizing：evaluator 已算好的 fair_value 直接喂 Kelly（conf=1.0）。"""
    market = context.market
    if market is None:
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=Decimal("0"),
                reason="series_missing_market",
            ),
            reason="series_missing_market",
        )

    sub_type, settings = series_subtype_settings(market, config, ports=ports)
    budget = settings.budget_usdc
    _series_meta: dict[str, str] = {"market_family": "series", "series_sub_type": sub_type.value}
    if budget <= Decimal("0"):
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=budget,
                reason=f"series_{sub_type.value}_budget_zero",
            ),
            reason=f"series_{sub_type.value}_budget_zero",
            metadata=_series_meta,
        )

    kelly_fraction = context.kelly_fraction
    kelly_max_position_fraction = context.kelly_max_position_fraction
    kelly_min_stake_usdc = context.kelly_min_stake_usdc
    if kelly_fraction is None or kelly_max_position_fraction is None or kelly_min_stake_usdc is None:
        logger.warning(
            "series_sizing.missing_kelly_params: trace_id=%s condition_id=%s",
            context.trace_id,
            market.condition_id,
        )
        return EntrySizing(
            allocation_plan=AllocationPlan(
                trace_id=context.trace_id,
                total_budget_usdc=budget,
                reason="series_sizing_no_kelly_params",
            ),
            reason="series_sizing_no_kelly_params",
            metadata={
                "market_family": "series",
                "series_budget_usdc": str(budget),
                "series_sub_type": sub_type.value,
                "kelly_path": "not_applied",
            },
        )

    metadata = context.metadata or {}
    now = context.now or datetime.now(timezone.utc)
    token_views = tuple(context.market_token_views or ())

    per_market_cap = min(budget, settings.max_per_market_usdc)
    _season_port = ports.season_state if ports is not None else None
    _season_snapshot = _season_port.season_snapshot() if _season_port is not None else None
    fair_value_by_token: dict[str, Decimal] = {}
    market_snapshots: list[AllocationMarketSnapshot] = []
    for tv in token_views:
        if not tv.token_id:
            continue
        ob = tv.orderbook
        best_ask = ob.best_ask if ob is not None else None
        buyable_usdc = (
            ob.buyable_ask_depth(max_price=settings.max_entry_price) * best_ask
            if ob is not None and best_ask is not None
            else Decimal("0")
        )
        evaluator_inputs = SeriesEvaluatorInputs(
            best_ask=best_ask,
            best_bid=ob.best_bid if ob is not None else None,
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
            outcome_label=tv.outcome or "",
            token_id=tv.token_id,
            metadata=metadata,
        )
        evaluation = evaluate_series_opportunity(candidate, inputs=evaluator_inputs)
        if evaluation.fair_value is not None:
            fair_value_by_token[tv.token_id] = evaluation.fair_value
        market_snapshots.append(AllocationMarketSnapshot(
            market=market,
            token_id=tv.token_id,
            orderbook=ob,
            best_ask=best_ask,
            strategy_budget_cap_usdc=per_market_cap,
        ))

    def _prob_provider(snap: AllocationMarketSnapshot) -> ProbView:
        fair = fair_value_by_token.get(snap.token_id)
        if fair is None:
            return ProbView(
                prob_p=None,
                prob_confidence=Decimal("0"),
                source=f"series_{sub_type.value}_no_fair_value",
            )
        return ProbView(prob_p=fair, prob_confidence=Decimal("1"), source=f"series_{sub_type.value}_real")

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
    return EntrySizing(allocation_plan=plan, reason=f"series_{sub_type.value}_kelly")
