"""CLOB API（orderbook / order / fill / price history）schema 层。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.order import (
    Order as OrderRecord,
    OrderSide,
    OrderStatus,
    OrderType,
)
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.infra.polymarket.base_client import _summary

from ._helpers import (
    _coerce_bool,
    _coerce_datetime,
    _coerce_decimal,
    _coerce_price_levels,
    _first_text,
    _first_value,
    _iter_mappings,
    _normalize_address,
    _unwrap_mapping,
    _utc_now,
)


@dataclass(frozen=True, slots=True)
class ClobOrderRequest:
    token_id: str
    side: str
    order_type: str
    price: Decimal
    amount: Decimal
    post_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "token_id", str(self.token_id).strip())
        object.__setattr__(self, "side", str(self.side).strip().upper())
        object.__setattr__(self, "order_type", str(self.order_type).strip().upper())
        object.__setattr__(self, "price", _coerce_decimal(self.price) or Decimal("0"))
        object.__setattr__(self, "amount", _coerce_decimal(self.amount) or Decimal("0"))
        object.__setattr__(self, "post_only", bool(self.post_only))

    def to_payload(self) -> dict[str, Any]:
        # BUY 的 amount 语义是花费的 USDC.e 金额；SELL 的 amount 语义是 shares。
        return {
            "token_id": self.token_id,
            "side": self.side,
            "order_type": self.order_type,
            "price": str(self.price),
            "amount": str(self.amount),
            "post_only": self.post_only,
        }


@dataclass(frozen=True, slots=True)
class OrderbookLevelDTO:
    price: Decimal
    size: Decimal

    def to_price_level(self) -> PriceLevel:
        return PriceLevel(price=self.price, size=self.size)


@dataclass(frozen=True, slots=True)
class ClobOrderbookDTO:
    raw: Mapping[str, Any]
    token_id: str
    bids: tuple[OrderbookLevelDTO, ...] = field(default_factory=tuple)
    asks: tuple[OrderbookLevelDTO, ...] = field(default_factory=tuple)
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    best_bid_size: Decimal | None = None
    best_ask_size: Decimal | None = None
    last_trade_price: Decimal | None = None
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    market_slug: str | None = None
    condition_id: str | None = None
    received_at: datetime = field(default_factory=_utc_now)
    raw_summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "token_id", str(self.token_id).strip())
        object.__setattr__(self, "market_slug", self.market_slug or _first_text(self.raw, "market_slug", "marketSlug", "slug"))
        object.__setattr__(self, "condition_id", self.condition_id or _first_text(self.raw, "condition_id", "conditionId", "condition"))
        object.__setattr__(self, "best_bid", self.best_bid if self.best_bid is not None else _coerce_decimal(_first_value(self.raw, "best_bid", "bestBid")))
        object.__setattr__(self, "best_ask", self.best_ask if self.best_ask is not None else _coerce_decimal(_first_value(self.raw, "best_ask", "bestAsk")))
        object.__setattr__(self, "best_bid_size", self.best_bid_size if self.best_bid_size is not None else _coerce_decimal(_first_value(self.raw, "best_bid_size", "bestBidSize")))
        object.__setattr__(self, "best_ask_size", self.best_ask_size if self.best_ask_size is not None else _coerce_decimal(_first_value(self.raw, "best_ask_size", "bestAskSize")))
        object.__setattr__(self, "last_trade_price", self.last_trade_price if self.last_trade_price is not None else _coerce_decimal(_first_value(self.raw, "last_trade_price", "lastTradePrice")))
        object.__setattr__(self, "tick_size", self.tick_size if self.tick_size is not None else _coerce_decimal(_first_value(self.raw, "tick_size", "tickSize")))
        object.__setattr__(self, "min_order_size", self.min_order_size if self.min_order_size is not None else _coerce_decimal(_first_value(self.raw, "min_order_size", "minOrderSize")))
        received_at = _coerce_datetime(_first_value(self.raw, "received_at", "receivedAt", "timestamp", "ts"))
        if received_at is not None:
            object.__setattr__(self, "received_at", received_at)
        summary = self.raw_summary.strip() if self.raw_summary else ""
        if not summary:
            summary = _summary(self.raw) or ""
        object.__setattr__(self, "raw_summary", summary)

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def to_snapshot(self) -> OrderbookSnapshot:
        return OrderbookSnapshot(
            token_id=self.token_id,
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            bids=tuple(level.to_price_level() for level in self.bids),
            asks=tuple(level.to_price_level() for level in self.asks),
            received_at=self.received_at,
            market_slug=self.market_slug,
            condition_id=self.condition_id,
            best_bid_size=self.best_bid_size,
            best_ask_size=self.best_ask_size,
            last_trade_price=self.last_trade_price,
            tick_size=self.tick_size,
        )


@dataclass(frozen=True, slots=True)
class ClobPriceHistoryPointDTO:
    raw: Mapping[str, Any]
    timestamp: datetime = field(default_factory=_utc_now)
    price: Decimal | None = None

    def __post_init__(self) -> None:
        timestamp = _coerce_datetime(_first_value(self.raw, "t", "timestamp", "ts"))
        if timestamp is not None:
            object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "price", self.price if self.price is not None else _coerce_decimal(_first_value(self.raw, "p", "price")))


@dataclass(frozen=True, slots=True)
class ClobPriceHistoryDTO:
    raw: Mapping[str, Any]
    history: tuple[ClobPriceHistoryPointDTO, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.history:
            points = tuple(
                ClobPriceHistoryPointDTO(raw=item)
                for item in _iter_mappings(self.raw, "history", "items", "results")
            )
            object.__setattr__(self, "history", points)


@dataclass(frozen=True, slots=True)
class ClobOrderDTO:
    raw: Mapping[str, Any]
    token_id: str
    side: OrderSide
    order_type: OrderType
    price: Decimal
    order_id: str | None = None
    market_slug: str | None = None
    condition_id: str | None = None
    amount_usdc: Decimal | None = None
    size_shares: Decimal | None = None
    filled_shares: Decimal = Decimal("0")
    remaining_shares: Decimal | None = None
    trade_id: str | None = None
    status: str = "created"
    reason: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    raw_summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "token_id", str(self.token_id).strip())
        object.__setattr__(self, "order_id", self.order_id or _first_text(self.raw, "order_id", "orderId", "id"))
        object.__setattr__(self, "market_slug", self.market_slug or _first_text(self.raw, "market_slug", "marketSlug", "slug"))
        object.__setattr__(
            self,
            "condition_id",
            self.condition_id or _first_text(self.raw, "condition_id", "conditionId", "condition", "market"),
        )
        object.__setattr__(self, "amount_usdc", self.amount_usdc if self.amount_usdc is not None else _coerce_decimal(_first_value(self.raw, "amount_usdc", "amount")))
        object.__setattr__(
            self,
            "size_shares",
            self.size_shares
            if self.size_shares is not None
            else _coerce_decimal(_first_value(self.raw, "size_shares", "size", "quantity", "original_size")),
        )
        object.__setattr__(
            self,
            "filled_shares",
            self.filled_shares
            if self.filled_shares is not None
            else _coerce_decimal(_first_value(self.raw, "filled_shares", "filledSize", "size_matched")) or Decimal("0"),
        )
        remaining_shares = self.remaining_shares
        if remaining_shares is None:
            remaining_shares = _coerce_decimal(_first_value(self.raw, "remaining_shares", "remainingSize"))
        if remaining_shares is None and self.size_shares is not None:
            remaining_shares = max(self.size_shares - self.filled_shares, Decimal("0"))
        object.__setattr__(self, "remaining_shares", remaining_shares)
        object.__setattr__(self, "trade_id", self.trade_id or _first_text(self.raw, "trade_id", "tradeId"))
        object.__setattr__(
            self,
            "status",
            self.status or _normalize_order_status_text(_first_text(self.raw, "status")) or "created",
        )
        object.__setattr__(self, "reason", self.reason or _first_text(self.raw, "reason", "message") or "")
        created_at = _coerce_datetime(_first_value(self.raw, "created_at", "createdAt", "timestamp"))
        if created_at is not None:
            object.__setattr__(self, "created_at", created_at)
        updated_at = _coerce_datetime(_first_value(self.raw, "updated_at", "updatedAt", "last_update", "lastUpdate"))
        if updated_at is not None:
            object.__setattr__(self, "updated_at", updated_at)
        summary = self.raw_summary.strip() if self.raw_summary else ""
        if not summary:
            summary = _summary(self.raw) or ""
        object.__setattr__(self, "raw_summary", summary)

    def to_order_record(self, *, strategy_id: str) -> OrderRecord:
        if not strategy_id:
            raise ValueError("to_order_record requires non-empty strategy_id")
        return OrderRecord(
            strategy_id=strategy_id,
            trace_id=_first_text(self.raw, "trace_id", "traceId") or "",
            condition_id=self.condition_id or "",
            token_id=self.token_id,
            market_slug=self.market_slug,
            side=self.side,
            order_type=self.order_type,
            price=self.price,
            amount_usdc=self.amount_usdc,
            size_shares=self.size_shares,
            filled_shares=self.filled_shares,
            remaining_shares=self.remaining_shares,
            trade_id=self.trade_id,
            order_id=self.order_id,
            status=_coerce_order_status(self.status),
            reason=self.reason,
            post_only=bool(_coerce_bool(_first_value(self.raw, "post_only", "postOnly"))),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


@dataclass(frozen=True, slots=True)
class ClobFillDTO:
    raw: Mapping[str, Any]
    token_id: str
    side: OrderSide
    price: Decimal
    size_shares: Decimal
    order_id: str | None = None
    trade_id: str | None = None
    condition_id: str | None = None
    market_slug: str | None = None
    notional_usdc: Decimal | None = None
    status: str = "confirmed"
    confirmed_at: datetime = field(default_factory=_utc_now)
    raw_summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "token_id", str(self.token_id).strip())
        order_id = self.order_id or _first_text(self.raw, "order_id", "orderId", "taker_order_id", "takerOrderId")
        if order_id is None:
            maker_orders = _first_value(self.raw, "maker_orders", "makerOrders")
            if isinstance(maker_orders, list) and maker_orders:
                first_order = maker_orders[0]
                if isinstance(first_order, Mapping):
                    order_id = _first_text(first_order, "order_id", "orderId", "id")
                else:
                    text = str(first_order).strip()
                    order_id = text or None
        object.__setattr__(self, "order_id", order_id)
        object.__setattr__(self, "trade_id", self.trade_id or _first_text(self.raw, "trade_id", "tradeId", "id"))
        object.__setattr__(
            self,
            "condition_id",
            self.condition_id or _first_text(self.raw, "condition_id", "conditionId", "condition", "market"),
        )
        object.__setattr__(self, "market_slug", self.market_slug or _first_text(self.raw, "market_slug", "marketSlug", "slug"))
        object.__setattr__(self, "notional_usdc", self.notional_usdc if self.notional_usdc is not None else _coerce_decimal(_first_value(self.raw, "notional_usdc", "notional", "spent_usdc", "spentUsdC")))
        object.__setattr__(
            self,
            "status",
            self.status or _normalize_trade_status_text(_first_text(self.raw, "status")) or "confirmed",
        )
        confirmed_at = _coerce_datetime(
            _first_value(self.raw, "confirmed_at", "confirmedAt", "timestamp", "match_time", "matchTime", "last_update", "lastUpdate")
        )
        if confirmed_at is not None:
            object.__setattr__(self, "confirmed_at", confirmed_at)
        summary = self.raw_summary.strip() if self.raw_summary else ""
        if not summary:
            summary = _summary(self.raw) or ""
        object.__setattr__(self, "raw_summary", summary)

    def to_fill(self, *, strategy_id: str) -> Fill:
        if not strategy_id:
            raise ValueError("to_fill requires non-empty strategy_id")
        return Fill(
            strategy_id=strategy_id,
            trace_id=_first_text(self.raw, "trace_id", "traceId") or "",
            order_id=self.order_id,
            trade_id=self.trade_id,
            condition_id=self.condition_id,
            token_id=self.token_id,
            market_slug=self.market_slug,
            side=self.side.value,
            price=self.price,
            size=self.size_shares,
            notional_usdc=self.notional_usdc or self.price * self.size_shares,
            status=self.status,
            confirmed_at=self.confirmed_at,
        )


def normalize_orderbook_payload(
    payload: Mapping[str, Any],
    *,
    token_id: str,
    market_slug: str | None = None,
    condition_id: str | None = None,
) -> ClobOrderbookDTO:
    normalized = _unwrap_mapping(payload)
    bids = _coerce_price_levels(_first_value(normalized, "bids", "buy"))
    asks = _coerce_price_levels(_first_value(normalized, "asks", "sell"))
    best_bid = _coerce_decimal(_first_value(normalized, "best_bid", "bestBid"))
    best_ask = _coerce_decimal(_first_value(normalized, "best_ask", "bestAsk"))
    if best_bid is None and bids:
        best_bid = max(level.price for level in bids)
    if best_ask is None and asks:
        best_ask = min(level.price for level in asks)
    best_bid_size = _coerce_decimal(_first_value(normalized, "best_bid_size", "bestBidSize"))
    best_ask_size = _coerce_decimal(_first_value(normalized, "best_ask_size", "bestAskSize"))
    if best_bid_size is None and bids:
        best_bid_size = max((level.size for level in bids if level.price == best_bid), default=None)
    if best_ask_size is None and asks:
        best_ask_size = min((level.size for level in asks if level.price == best_ask), default=None)
    last_trade_price = _coerce_decimal(_first_value(normalized, "last_trade_price", "lastTradePrice"))
    tick_size = _coerce_decimal(_first_value(normalized, "tick_size", "tickSize"))
    min_order_size = _coerce_decimal(_first_value(normalized, "min_order_size", "minOrderSize"))
    received_at = _coerce_datetime(_first_value(normalized, "received_at", "receivedAt", "timestamp", "ts"))
    return ClobOrderbookDTO(
        raw=normalized,
        token_id=token_id,
        bids=tuple(OrderbookLevelDTO(level.price, level.size) for level in bids),
        asks=tuple(OrderbookLevelDTO(level.price, level.size) for level in asks),
        best_bid=best_bid,
        best_ask=best_ask,
        best_bid_size=best_bid_size,
        best_ask_size=best_ask_size,
        last_trade_price=last_trade_price,
        tick_size=tick_size,
        min_order_size=min_order_size,
        market_slug=market_slug,
        condition_id=condition_id,
        received_at=received_at or _utc_now(),
    )


def normalize_price_history_payload(payload: Mapping[str, Any]) -> ClobPriceHistoryDTO:
    normalized = _unwrap_mapping(payload)
    return ClobPriceHistoryDTO(raw=normalized)


def orderbook_payload_to_snapshot(
    payload: Mapping[str, Any],
    *,
    token_id: str,
    market_slug: str | None = None,
    condition_id: str | None = None,
) -> OrderbookSnapshot:
    return normalize_orderbook_payload(
        payload,
        token_id=token_id,
        market_slug=market_slug,
        condition_id=condition_id,
    ).to_snapshot()


def orderbook_to_domain_snapshot(payload: Mapping[str, Any], *, token_id: str) -> OrderbookSnapshot:
    return normalize_orderbook_payload(payload, token_id=token_id).to_snapshot()


def normalize_order_payload(payload: Mapping[str, Any]) -> ClobOrderDTO:
    normalized = _unwrap_mapping(payload)
    side = _first_text(normalized, "side") or OrderSide.BUY.value
    order_type = _first_text(normalized, "order_type", "orderType") or OrderType.GTC.value
    return ClobOrderDTO(
        raw=normalized,
        token_id=_first_text(normalized, "token_id", "tokenId", "asset_id", "assetId") or "",
        side=OrderSide(side.upper()),
        order_type=OrderType(order_type.upper()),
        price=_coerce_decimal(_first_value(normalized, "price")) or Decimal("0"),
        order_id=_first_text(normalized, "order_id", "orderId", "id"),
        market_slug=_first_text(normalized, "market_slug", "marketSlug", "slug"),
        condition_id=_first_text(normalized, "condition_id", "conditionId", "condition", "market"),
        amount_usdc=_coerce_decimal(_first_value(normalized, "amount_usdc", "amount")),
        size_shares=_coerce_decimal(_first_value(normalized, "size_shares", "size", "quantity", "original_size")),
        filled_shares=_coerce_decimal(_first_value(normalized, "filled_shares", "filledSize", "size_matched")) or Decimal("0"),
        remaining_shares=_coerce_decimal(_first_value(normalized, "remaining_shares", "remainingSize")),
        trade_id=_first_text(normalized, "trade_id", "tradeId"),
        status=_normalize_order_status_text(_first_text(normalized, "status")) or "created",
        reason=_first_text(normalized, "reason", "message") or "",
        created_at=_coerce_datetime(_first_value(normalized, "created_at", "createdAt")) or _utc_now(),
        updated_at=_coerce_datetime(_first_value(normalized, "updated_at", "updatedAt", "last_update", "lastUpdate")) or _utc_now(),
    )


def normalize_fill_payload(
    payload: Mapping[str, Any],
    *,
    user_address: str | None = None,
) -> ClobFillDTO:
    normalized = _unwrap_mapping(payload)
    maker_order = _maker_fill_order(normalized, user_address=user_address)
    if maker_order is not None:
        parent_side = (_first_text(normalized, "side") or "").upper()
        maker_side = _first_text(maker_order, "side", "order_side", "orderSide")
        if maker_side is None:
            maker_side = _opposite_trade_side(parent_side)
        return ClobFillDTO(
            raw=normalized,
            token_id=_first_text(maker_order, "token_id", "tokenId", "asset_id", "assetId")
            or _first_text(normalized, "token_id", "tokenId", "asset_id", "assetId")
            or "",
            side=OrderSide((maker_side or OrderSide.BUY.value).upper()),
            price=_coerce_decimal(_first_value(maker_order, "price"))
            or _coerce_decimal(_first_value(normalized, "price", "trade_price", "tradePrice"))
            or Decimal("0"),
            size_shares=_coerce_decimal(
                _first_value(
                    maker_order,
                    "matched_amount",
                    "matchedAmount",
                    "matched_size",
                    "matchedSize",
                    "filled_size",
                    "filledSize",
                    "size",
                    "quantity",
                )
            )
            or Decimal("0"),
            order_id=_first_text(maker_order, "order_id", "orderId", "id"),
            trade_id=_first_text(normalized, "trade_id", "tradeId", "id"),
            condition_id=_first_text(maker_order, "condition_id", "conditionId", "condition", "market")
            or _first_text(normalized, "condition_id", "conditionId", "condition", "market"),
            market_slug=_first_text(maker_order, "market_slug", "marketSlug", "slug")
            or _first_text(normalized, "market_slug", "marketSlug", "slug"),
            notional_usdc=_coerce_decimal(_first_value(maker_order, "notional_usdc", "notional", "value")),
            status=_normalize_trade_status_text(_first_text(normalized, "status")) or "confirmed",
            confirmed_at=_coerce_datetime(
                _first_value(
                    normalized,
                    "confirmed_at",
                    "confirmedAt",
                    "match_time",
                    "matchTime",
                    "last_update",
                    "lastUpdate",
                )
            )
            or _utc_now(),
        )
    side_text = _first_text(normalized, "side") or OrderSide.BUY.value
    return ClobFillDTO(
        raw=normalized,
        token_id=_first_text(normalized, "token_id", "tokenId", "asset_id", "assetId") or "",
        side=OrderSide(side_text.upper()),
        price=_coerce_decimal(_first_value(normalized, "price", "trade_price", "tradePrice")) or Decimal("0"),
        size_shares=_coerce_decimal(_first_value(normalized, "size_shares", "size", "quantity")) or Decimal("0"),
        order_id=_first_text(normalized, "order_id", "orderId", "taker_order_id", "takerOrderId"),
        trade_id=_first_text(normalized, "trade_id", "tradeId", "id"),
        condition_id=_first_text(normalized, "condition_id", "conditionId", "condition", "market"),
        market_slug=_first_text(normalized, "market_slug", "marketSlug", "slug"),
        notional_usdc=_coerce_decimal(_first_value(normalized, "notional_usdc", "notional", "value")),
        status=_normalize_trade_status_text(_first_text(normalized, "status")) or "confirmed",
        confirmed_at=_coerce_datetime(_first_value(normalized, "confirmed_at", "confirmedAt", "match_time", "matchTime", "last_update", "lastUpdate")) or _utc_now(),
    )


def _maker_fill_order(
    payload: Mapping[str, Any],
    *,
    user_address: str | None,
) -> Mapping[str, Any] | None:
    """返回用户作为 maker 时应计入我方账户的 maker order 腿。"""

    trader_side = (_first_text(payload, "trader_side", "traderSide") or "").lower()
    maker_orders = _first_value(payload, "maker_orders", "makerOrders")
    if trader_side != "maker" or not isinstance(maker_orders, list):
        return None
    orders = tuple(item for item in maker_orders if isinstance(item, Mapping))
    normalized_user_address = _normalize_address(user_address)
    if normalized_user_address is not None:
        for item in orders:
            maker_address = _normalize_address(_first_text(item, "maker_address", "makerAddress"))
            if maker_address == normalized_user_address:
                return item
        return None
    if len(orders) == 1:
        return orders[0]
    return None


def _opposite_trade_side(side: str) -> str:
    if side.upper() == OrderSide.BUY.value:
        return OrderSide.SELL.value
    if side.upper() == OrderSide.SELL.value:
        return OrderSide.BUY.value
    return ""


def _coerce_order_status(value: str) -> OrderStatus:
    text = value.strip().lower()
    if text in {"created", "new"}:
        return OrderStatus.CREATED
    if text in {"signed", "signing"}:
        return OrderStatus.SIGNED
    if text in {"submitted", "open"}:
        return OrderStatus.SUBMITTED
    if text in {"cancel_requested", "cancel-requested"}:
        return OrderStatus.CANCEL_REQUESTED
    if text in {"matched", "match"}:
        return OrderStatus.MATCHED
    if text in {"partially_filled", "partial_fill", "partial-filled"}:
        return OrderStatus.PARTIALLY_FILLED
    if text in {"no_fill", "no-fill", "unfilled"}:
        return OrderStatus.NO_FILL
    if text in {"live", "resting", "open_live"}:
        return OrderStatus.LIVE
    if text in {"cancelled", "canceled"}:
        return OrderStatus.CANCELLED
    if text in {"rejected", "reject"}:
        return OrderStatus.REJECTED
    if text in {"failed", "error"}:
        return OrderStatus.FAILED
    return OrderStatus.CREATED


def _normalize_order_status_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text.startswith("order_status_"):
        text = text.removeprefix("order_status_")
    aliases = {
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "delayed": "submitted",
        "open": "submitted",
        "unmatched": "no_fill",
        "live": "live",
        "matched": "matched",
        "partially_filled": "partially_filled",
        "partial_filled": "partially_filled",
        "rejected": "rejected",
        "failed": "failed",
    }
    return aliases.get(text, text)


def _normalize_trade_status_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text.startswith("trade_status_"):
        text = text.removeprefix("trade_status_")
    aliases = {
        "confirmed": "confirmed",
        "matched": "matched",
        "mined": "mined",
        "retrying": "retrying",
        "failed": "failed",
    }
    return aliases.get(text, text)
