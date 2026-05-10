from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.infra.polymarket import market_ws_adapter
from polymarket_trader.runtime.registry import MarketRegistry

_first = market_ws_adapter.first_value
_mapping = market_ws_adapter.nested_mapping
_bool = market_ws_adapter.bool_value
_bps = market_ws_adapter.bps_value
_fee_rate_units = market_ws_adapter.fee_rate_units

DecimalSerializer = Callable[[Decimal | None], str | None]


class MarketWsMarketUpdater:
    def __init__(
        self,
        *,
        registry: MarketRegistry | None,
        tracked_markets: dict[str, Market],
        serialize_decimal: DecimalSerializer,
    ) -> None:
        self._registry = registry
        self._tracked_markets = tracked_markets
        self._serialize_decimal = serialize_decimal

    def market_payload(self, market: Market) -> dict[str, Any]:
        return {
            "condition_id": market.condition_id,
            "market_slug": market.market_slug,
            "event_slug": market.event_slug,
            "event_id": market.event_id,
            "event_title": market.event_title,
            "token_ids": list(market.token_ids),
            "outcomes": [
                {
                    "token_id": outcome.token_id,
                    "outcome": outcome.outcome,
                }
                for outcome in market.outcomes
            ],
            "tick_size": self._serialize_decimal(market.tick_size),
            "min_order_size": self._serialize_decimal(market.min_order_size),
            "neg_risk": market.neg_risk,
            "fees": {
                "enabled": market.fees_enabled,
                "maker_base_fee_bps": market.maker_base_fee_bps,
                "taker_base_fee_bps": market.taker_base_fee_bps,
                "fee_rate_bps": market.fee_rate_bps,
                "fee_rate_updated_at": (
                    None
                    if market.fee_rate_updated_at is None
                    else market.fee_rate_updated_at.isoformat()
                ),
            },
            "category": market.category,
            "tags": list(market.tags),
            "matched_keywords": list(market.matched_keywords),
            "trading_status": market.trading_status.value,
            "reject_reason": market.reject_reason,
        }

    def fee_rate_bps_from_message(self, message: Mapping[str, Any]) -> int | None:
        return _bps(_first(message, "fee_rate_bps", "feeRateBps"))

    def update_fee_schedule(
        self,
        token_id: str,
        market: Market,
        message: Mapping[str, Any],
    ) -> Market | None:
        fee_schedule = _mapping(message, "fee_schedule", "feeSchedule")
        fees_enabled = _bool(_first(message, "fees_enabled", "feesEnabled"))
        if fees_enabled is None and fee_schedule is not None:
            fees_enabled = _bool(_first(fee_schedule, "enabled", "feesEnabled"))
        maker_base_fee_bps = _bps(
            _first(
                message,
                "maker_base_fee_bps",
                "makerBaseFee",
                "maker_base_fee",
            )
        )
        taker_base_fee_bps = (
            _fee_rate_units(_first(fee_schedule, "rate", "base_fee", "baseFee"))
            if fee_schedule is not None
            else None
        )
        if taker_base_fee_bps is None:
            taker_base_fee_bps = _bps(
                _first(
                    message,
                    "taker_base_fee_bps",
                    "takerBaseFee",
                    "taker_base_fee",
                )
            )
        updated_market = market.with_fee_schedule(
            fees_enabled=fees_enabled,
            maker_base_fee_bps=maker_base_fee_bps,
            taker_base_fee_bps=taker_base_fee_bps,
        )
        if updated_market == market:
            return None
        if self._registry is not None:
            refreshed = self._registry.update_fee_schedule(
                market.condition_id,
                fees_enabled=updated_market.fees_enabled,
                maker_base_fee_bps=updated_market.maker_base_fee_bps,
                taker_base_fee_bps=updated_market.taker_base_fee_bps,
            )
            if refreshed is not None:
                updated_market = refreshed
        self._tracked_markets[token_id] = updated_market
        return updated_market

    def update_fee_rate(
        self,
        token_id: str,
        market: Market,
        fee_rate_bps: int,
        *,
        updated_at: datetime,
    ) -> Market | None:
        if market.fee_rate_bps is not None or market.taker_base_fee_bps is not None:
            return None
        if market.fee_rate_bps == fee_rate_bps and market.fee_rate_updated_at is not None:
            return None
        updated_market = market.with_fee_rate(
            fee_rate_bps,
            fee_rate_updated_at=updated_at,
        )
        if updated_market == market:
            return None
        if self._registry is not None:
            refreshed = self._registry.update_fee_rate(
                market.condition_id,
                fee_rate_bps,
                fee_rate_updated_at=updated_at,
            )
            if refreshed is not None:
                updated_market = refreshed
        self._tracked_markets[token_id] = updated_market
        return updated_market
