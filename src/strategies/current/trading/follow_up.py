"""成交后续动作决策：profit-take 和 auto-exit。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.order import ManagedOrderIntent
from polymarket_trader.extension_api import DecisionKind, ExtensionContext, ExtensionDecision

from strategies.current.config import CurrentStrategyConfig
from strategies.current.exit_plan import cap_price_to_clob_limit, build_exit_plan_metadata, exit_price_for_context
from strategies.current.trading.helpers import enrich_decision


def decide_follow_up(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> tuple[ExtensionDecision, ...]:
    """根据 BUY 成交结果生成后续动作（profit-take 或 auto-exit）。"""
    if context.order_result is None:
        return ()
    if context.order_result.side is None or context.order_result.side.value != "BUY":
        return ()
    if context.order_result.matched_shares <= 0:
        return ()
    intent_metadata = _order_intent_metadata(context.order_result.intent)
    if _has_profit_take_follow_up(intent_metadata):
        target_price = _decimal_from_intent_metadata(intent_metadata.get("profit_take_target_price"))
        if target_price is None:
            return ()
        target_price = cap_price_to_clob_limit(target_price, tick_size=_effective_tick_size(context))
        exit_metadata = build_exit_plan_metadata(
            config,
            context,
            token_id=context.order_result.token_id,
            source_reason="profit_take_after_buy_fill",
            target_size_shares=context.order_result.matched_shares,
        )
        exit_metadata.update(intent_metadata)
        exit_plan = exit_metadata.get("exit_plan")
        if isinstance(exit_plan, dict):
            exit_plan["target_exit_price"] = str(target_price)
            exit_plan["primary_action"] = "place_profit_take_gtc_sell_after_buy_fill"
            exit_plan["settlement_rule"] = "keep_profit_take_order_until_fill_or_authoritative_resolution"
            exit_plan["recovery_rule"] = "cancel_open_entry_orders_and_cover_profit_take_positions"
        exit_metadata["exit_target_price"] = str(target_price)
        exit_metadata["exit_source_reason"] = "profit_take_after_buy_fill"
        return (
            enrich_decision(
                ExtensionDecision.sell(
                    reason="strategy_profit_take",
                    token_id=context.order_result.token_id,
                    price=target_price,
                    size_shares=context.order_result.matched_shares,
                    market_slug=context.order_result.market_slug or (
                        context.market.market_slug if context.market is not None else None
                    ),
                    metadata=exit_metadata,
                ),
                default_kind=DecisionKind.FOLLOW_UP,
            ),
        )
    if not config.auto_exit_enabled:
        return ()
    return (
        enrich_decision(
            ExtensionDecision.sell(
                reason="strategy_exit",
                token_id=context.order_result.token_id,
                price=exit_price_for_context(config, context),
                size_shares=context.order_result.matched_shares,
                market_slug=context.order_result.market_slug or (
                    context.market.market_slug if context.market is not None else None
                ),
                metadata=build_exit_plan_metadata(
                    config,
                    context,
                    token_id=context.order_result.token_id,
                    source_reason="follow_up_after_buy_fill",
                    target_size_shares=context.order_result.matched_shares,
                ),
            ),
            default_kind=DecisionKind.FOLLOW_UP,
        ),
    )


def _order_intent_metadata(intent: ManagedOrderIntent | None) -> Mapping[str, Any]:
    if intent is None:
        return {}
    return intent.metadata


def _has_profit_take_follow_up(metadata: Mapping[str, object]) -> bool:
    return metadata.get("exit_mode") == "profit_take" or bool(
        metadata.get("profit_take_overlay_enabled")
    )


def _effective_tick_size(context: ExtensionContext) -> Decimal | None:
    if context.orderbook is not None and context.orderbook.tick_size is not None:
        return context.orderbook.tick_size
    if context.market is not None:
        return context.market.tick_size
    return None


def _decimal_from_intent_metadata(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
