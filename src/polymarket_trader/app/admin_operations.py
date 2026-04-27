from __future__ import annotations

from typing import Sequence

from polymarket_trader.domain.market import Market, TradingStatus


def market_status_allowed_for_manual_order(market: Market) -> bool:
    return market.trading_status not in {
        TradingStatus.CLOSED,
        TradingStatus.RESOLVED,
        TradingStatus.REJECTED,
    }


def normalize_condition_ids(condition_ids: Sequence[str] | None) -> tuple[str, ...]:
    if not condition_ids:
        return ()
    return tuple(condition_id for condition_id in condition_ids if condition_id)
