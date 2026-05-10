"""Polymarket schema 包。

把原 ``schemas.py`` 拆为 _helpers / gamma / clob / data / ws 子模块，并通过
``__init__`` 重导出原有公开符号，调用方继续按
``from polymarket_trader.infra.polymarket.schemas import X`` 使用。

错误类型从 ``base_client`` 经此模块再导出，保持原 schemas.py 的 __all__ 兼容。
"""

from __future__ import annotations

from polymarket_trader.domain.discovery import RawMarketEvent
from polymarket_trader.infra.polymarket.base_client import (
    PolymarketAuthError,
    PolymarketClientError,
    PolymarketRateLimitError,
    PolymarketResponseError,
    PolymarketRestClientBase,
    PolymarketTimeoutError,
    PolymarketTransportError,
    PolymarketWebSocketError,
)

from .clob import (
    ClobFillDTO,
    ClobOrderDTO,
    ClobOrderRequest,
    ClobOrderbookDTO,
    ClobPriceHistoryDTO,
    ClobPriceHistoryPointDTO,
    OrderbookLevelDTO,
    normalize_fill_payload,
    normalize_order_payload,
    normalize_orderbook_payload,
    normalize_price_history_payload,
    orderbook_payload_to_snapshot,
    orderbook_to_domain_snapshot,
)
from .data import (
    BalanceAllowanceDTO,
    DataPositionDTO,
    data_position_to_domain_position,
    normalize_balance_allowance_payload,
    normalize_position_payload,
)
from .gamma import (
    GammaEventDTO,
    GammaMarketDTO,
    GammaPublicProfileDTO,
    gamma_event_to_raw_market_events,
    normalize_gamma_event,
    normalize_gamma_market,
    normalize_gamma_public_profile,
)
from .ws import (
    PolymarketSubscriptionChannel,
    WebSocketMessage,
    WebSocketSubscription,
    build_market_subscription_request,
    build_user_subscription_request,
    parse_ws_message,
    parse_ws_messages,
)

__all__ = [
    "BalanceAllowanceDTO",
    "ClobFillDTO",
    "ClobOrderDTO",
    "ClobOrderRequest",
    "ClobOrderbookDTO",
    "ClobPriceHistoryDTO",
    "ClobPriceHistoryPointDTO",
    "DataPositionDTO",
    "GammaEventDTO",
    "GammaMarketDTO",
    "GammaPublicProfileDTO",
    "OrderbookLevelDTO",
    "PolymarketAuthError",
    "PolymarketClientError",
    "PolymarketRateLimitError",
    "PolymarketResponseError",
    "PolymarketRestClientBase",
    "PolymarketSubscriptionChannel",
    "PolymarketTimeoutError",
    "PolymarketTransportError",
    "PolymarketWebSocketError",
    "RawMarketEvent",
    "WebSocketMessage",
    "WebSocketSubscription",
    "build_market_subscription_request",
    "build_user_subscription_request",
    "data_position_to_domain_position",
    "gamma_event_to_raw_market_events",
    "normalize_balance_allowance_payload",
    "normalize_fill_payload",
    "normalize_gamma_event",
    "normalize_gamma_market",
    "normalize_gamma_public_profile",
    "normalize_order_payload",
    "normalize_orderbook_payload",
    "normalize_price_history_payload",
    "normalize_position_payload",
    "orderbook_payload_to_snapshot",
    "orderbook_to_domain_snapshot",
    "parse_ws_message",
    "parse_ws_messages",
]
