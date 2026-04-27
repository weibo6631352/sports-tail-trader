from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    ManagedOrderIntent,
    OrderType,
    ReplaceOrderIntent,
    SellOrderIntent,
    TradableOrderIntent,
)
from polymarket_trader.extension_api import ExtensionAction, ExtensionDecision


def decision_to_trade_intent(
    *,
    trace_id: str,
    market: Market,
    default_token_id: str | None,
    decision: ExtensionDecision,
) -> TradableOrderIntent | None:
    intent = decision_to_managed_intent(
        trace_id=trace_id,
        condition_id=market.condition_id,
        market_slug=market.market_slug,
        default_token_id=default_token_id,
        decision=decision,
    )
    if isinstance(intent, (BuyOrderIntent, SellOrderIntent)):
        return intent
    return None


def decision_to_managed_intent(
    *,
    trace_id: str,
    condition_id: str,
    market_slug: str | None,
    default_token_id: str | None,
    decision: ExtensionDecision,
) -> ManagedOrderIntent | None:
    resolved_token_id = decision.token_id or default_token_id
    if resolved_token_id is None:
        return None
    resolved_market_slug = decision.market_slug or market_slug
    if decision.action == ExtensionAction.BUY:
        if (
            decision.price is None
            or decision.amount_usdc is None
            or decision.amount_usdc <= Decimal("0")
        ):
            return None
        return BuyOrderIntent(
            trace_id=trace_id,
            condition_id=condition_id,
            token_id=resolved_token_id,
            price=decision.price,
            amount_usdc=decision.amount_usdc,
            order_type=decision.order_type or OrderType.FAK,
            market_slug=resolved_market_slug,
        )
    if decision.action == ExtensionAction.SELL:
        if decision.price is None or decision.size_shares is None or decision.size_shares <= Decimal("0"):
            return None
        return SellOrderIntent(
            trace_id=trace_id,
            condition_id=condition_id,
            token_id=resolved_token_id,
            price=decision.price,
            size_shares=decision.size_shares,
            order_type=decision.order_type or OrderType.GTC,
            market_slug=resolved_market_slug,
        )
    if decision.action == ExtensionAction.CANCEL:
        if not decision.order_id:
            return None
        return CancelOrderIntent(
            trace_id=trace_id,
            condition_id=condition_id,
            token_id=resolved_token_id,
            order_id=decision.order_id,
            market_slug=resolved_market_slug,
            reason=decision.reason,
        )
    if decision.action == ExtensionAction.REPLACE:
        if (
            not decision.order_id
            or decision.price is None
            or decision.size_shares is None
            or decision.size_shares <= Decimal("0")
        ):
            return None
        return ReplaceOrderIntent(
            trace_id=trace_id,
            condition_id=condition_id,
            token_id=resolved_token_id,
            order_id=decision.order_id,
            new_price=decision.price,
            size_shares=decision.size_shares,
            market_slug=resolved_market_slug,
            reason=decision.reason,
        )
    return None
