"""当前策略明确声明自己关注的 outcome。"""

from __future__ import annotations

from polymarket_trader.domain.market import Market

PRIMARY_OUTCOME = "NO"


def primary_token_id(market: Market) -> str:
    return market.require_token_id(PRIMARY_OUTCOME)


def is_primary_token(market: Market, token_id: str | None) -> bool:
    if token_id is None:
        return False
    return token_id == primary_token_id(market)
