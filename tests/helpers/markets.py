from __future__ import annotations

from typing import Any

from polymarket_trader.domain.market import Market, MarketOutcome


def binary_market_outcomes(
    *,
    no_token_id: str,
    yes_token_id: str,
) -> tuple[MarketOutcome, ...]:
    return (
        MarketOutcome(token_id=yes_token_id, outcome="YES"),
        MarketOutcome(token_id=no_token_id, outcome="NO"),
    )


def build_binary_market(
    *,
    no_token_id: str,
    yes_token_id: str,
    **kwargs: Any,
) -> Market:
    return Market(
        outcomes=binary_market_outcomes(
            no_token_id=no_token_id,
            yes_token_id=yes_token_id,
        ),
        **kwargs,
    )
