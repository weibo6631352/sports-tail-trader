from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from uuid import uuid4

from polymarket_trader.domain.events import DomainEvent, Fill
from polymarket_trader.domain.order import Order, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.position import Position


def first_value(mapping: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def decimal_value(value: Any | None, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return default
    return Decimal(text)


def datetime_value(value: Any | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = Decimal(text)
    except InvalidOperation:
        numeric = None
    if numeric is not None:
        timestamp = float(numeric)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def text_value(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalized_status(value: Any | None) -> str:
    text = text_value(value)
    return "" if text is None else text.lower()


def normalize_order_status(value: Any | None) -> OrderStatus:
    text = normalized_status(value)
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


def normalize_order_type(value: Any | None, *, default: OrderType = OrderType.GTC) -> OrderType:
    text = normalized_status(value)
    if text == "fak":
        return OrderType.FAK
    if text == "gtc":
        return OrderType.GTC
    return default


def normalize_user_order_status(message: Mapping[str, Any]) -> OrderStatus:
    explicit = text_value(first_value(message, "status", "order_status", "orderStatus"))
    if explicit is not None:
        return normalize_order_status(explicit)
    event_kind = normalized_status(first_value(message, "type", "action"))
    matched = decimal_value(
        first_value(message, "filled_shares", "size_matched", "matched_amount"),
        default=Decimal("0"),
    ) or Decimal("0")
    original = decimal_value(first_value(message, "size_shares", "size", "original_size", "quantity"))
    if event_kind in {"cancellation", "cancel", "cancelled", "canceled"}:
        return OrderStatus.CANCELLED
    if original is not None and original > 0 and matched >= original:
        return OrderStatus.MATCHED
    if matched > 0:
        return OrderStatus.PARTIALLY_FILLED
    if event_kind in {"placement", "update"}:
        return OrderStatus.LIVE
    return OrderStatus.CREATED


def message_type(message: Mapping[str, Any]) -> str:
    value = first_value(message, "event_type", "message_type", "channel_event", "type", "action")
    return normalized_status(value)


def extract_condition_id(message: Mapping[str, Any]) -> str | None:
    return text_value(first_value(message, "condition_id", "conditionId", "condition", "market"))


def extract_token_id(message: Mapping[str, Any]) -> str | None:
    return text_value(first_value(message, "token_id", "tokenId", "asset_id", "assetId", "market_token_id"))


def extract_market_slug(message: Mapping[str, Any]) -> str | None:
    return text_value(first_value(message, "market_slug", "marketSlug", "slug"))


def extract_trace_id(message: Mapping[str, Any]) -> str:
    value = text_value(first_value(message, "trace_id", "traceId"))
    return value or uuid4().hex


def extract_event_id(message: Mapping[str, Any]) -> str:
    value = text_value(first_value(message, "event_id", "eventId", "id"))
    return value or uuid4().hex


def flatten_message(message: Mapping[str, Any] | DomainEvent) -> Mapping[str, Any]:
    if isinstance(message, DomainEvent):
        payload = dict(message.payload)
        payload.setdefault("trace_id", message.trace_id)
        payload.setdefault("event_id", message.event_id)
        payload.setdefault("event_type", str(message.event_type))
        payload.setdefault("market_slug", message.market_slug)
        payload.setdefault("condition_id", message.condition_id)
        payload.setdefault("token_id", message.token_id)
        payload.setdefault("reason", message.reason)
        return payload
    return message


def decimal_or_zero(value: Any | None) -> Decimal:
    return decimal_value(value, default=Decimal("0")) or Decimal("0")


def coalesce_decimal(*values: Any | None, default: Decimal = Decimal("0")) -> Decimal:
    for value in values:
        coerced = decimal_value(value)
        if coerced is not None:
            return coerced
    return default


def is_mapping_sequence(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(item, Mapping) for item in value)


def is_snapshot_message(payload: Mapping[str, Any]) -> bool:
    kind = message_type(payload)
    return kind in {"snapshot", "sync", "initial_snapshot", "full_snapshot", "state"}


def iter_order_snapshots(payload: Mapping[str, Any]) -> Iterable[Order]:
    candidates: Iterable[Any]
    if is_mapping_sequence(payload.get("orders")):
        candidates = payload["orders"]
    elif is_mapping_sequence(payload.get("open_orders")):
        candidates = payload["open_orders"]
    elif isinstance(payload.get("order"), Mapping):
        candidates = (payload["order"],)
    else:
        candidates = (payload,)

    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        condition_id = extract_condition_id(item)
        token_id = extract_token_id(item)
        if condition_id is None or token_id is None:
            continue
        order_type = normalize_order_type(item.get("order_type"), default=OrderType.GTC)
        side_text = normalized_status(item.get("side"))
        if side_text == "buy":
            side = OrderSide.BUY
        elif side_text == "sell":
            side = OrderSide.SELL
        elif item.get("amount_usdc") is not None or item.get("amount") is not None:
            side = OrderSide.BUY
        else:
            side = OrderSide.SELL
        price = decimal_value(item.get("price"), default=Decimal("0")) or Decimal("0")
        amount_usdc = decimal_value(item.get("amount_usdc") or item.get("amount"))
        size_shares = decimal_value(item.get("size_shares") or item.get("size") or item.get("original_size"))
        filled_shares = decimal_value(
            item.get("filled_shares") or item.get("size_matched") or item.get("matched_amount"),
            default=Decimal("0"),
        ) or Decimal("0")
        notional_usdc = decimal_value(item.get("notional_usdc"))
        if notional_usdc is None and price is not None and size_shares is not None:
            notional_usdc = price * size_shares
        remaining_shares = decimal_value(item.get("remaining_shares") or item.get("remaining_size"))
        if remaining_shares is None and size_shares is not None:
            remaining_shares = max(Decimal("0"), size_shares - filled_shares)
        yield Order(
            trace_id=extract_trace_id(item),
            condition_id=condition_id,
            token_id=token_id,
            market_slug=extract_market_slug(item),
            side=side,
            order_type=order_type,
            price=price,
            amount_usdc=amount_usdc,
            size_shares=size_shares,
            filled_shares=filled_shares,
            notional_usdc=notional_usdc,
            order_id=text_value(item.get("order_id") or item.get("id")),
            trade_id=text_value(item.get("trade_id")),
            status=normalize_user_order_status(item),
            idempotency_key=text_value(item.get("idempotency_key")),
            reason=text_value(item.get("reason")) or "",
            post_only=bool(item.get("post_only", False)),
            created_at=datetime_value(item.get("created_at") or item.get("timestamp")),
            updated_at=datetime_value(
                item.get("updated_at") or item.get("last_update") or item.get("timestamp")
            ),
            remaining_shares=remaining_shares,
        )


def iter_position_snapshots(payload: Mapping[str, Any]) -> Iterable[Position]:
    candidates: Iterable[Any]
    if is_mapping_sequence(payload.get("positions")):
        candidates = payload["positions"]
    elif isinstance(payload.get("position"), Mapping):
        candidates = (payload["position"],)
    elif isinstance(payload.get("holdings"), Mapping):
        candidates = (payload["holdings"],)
    elif isinstance(payload.get("holdings"), list):
        candidates = payload["holdings"]
    else:
        candidates = (payload,)

    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        condition_id = extract_condition_id(item)
        token_id = extract_token_id(item)
        if condition_id is None or token_id is None:
            continue
        yield Position(
            condition_id=condition_id,
            token_id=token_id,
            market_slug=extract_market_slug(item),
            shares=coalesce_decimal(item.get("shares"), item.get("position_shares"), default=Decimal("0")),
            cost_usdc=coalesce_decimal(
                item.get("cost_usdc"),
                item.get("cost"),
                item.get("avg_cost_usdc"),
                default=Decimal("0"),
            ),
            open_buy_shares=coalesce_decimal(item.get("open_buy_shares"), default=Decimal("0")),
            open_sell_shares=coalesce_decimal(item.get("open_sell_shares"), default=Decimal("0")),
            pending_buy_shares=coalesce_decimal(item.get("pending_buy_shares"), default=Decimal("0")),
            confirmed_shares=coalesce_decimal(item.get("confirmed_shares"), default=Decimal("0")),
            last_order_id=text_value(item.get("last_order_id")),
            last_trade_id=text_value(item.get("last_trade_id")),
            confirmation_status=normalized_status(item.get("confirmation_status")) or "unknown",
            updated_at=datetime_value(item.get("updated_at") or item.get("timestamp")),
        )


def iter_fill_snapshots(payload: Mapping[str, Any]) -> Iterable[Fill]:
    candidates: Iterable[Any]
    if is_mapping_sequence(payload.get("fills")):
        candidates = payload["fills"]
    elif is_mapping_sequence(payload.get("trades")):
        candidates = payload["trades"]
    elif isinstance(payload.get("fill"), Mapping):
        candidates = (payload["fill"],)
    elif isinstance(payload.get("trade"), Mapping):
        candidates = (payload["trade"],)
    else:
        candidates = (payload,)

    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        condition_id = extract_condition_id(item)
        token_id = extract_token_id(item)
        if condition_id is None or token_id is None:
            continue
        size = coalesce_decimal(
            item.get("size"),
            item.get("filled_size"),
            item.get("quantity"),
            item.get("matched_amount"),
        )
        price = coalesce_decimal(item.get("price"), item.get("avg_price"))
        side_text = normalized_status(item.get("side"))
        status = normalized_status(item.get("status")) or normalized_status(item.get("trade_status"))
        if not status:
            status = "confirmed" if item.get("confirmed", False) else "matched"
        notional_usdc = coalesce_decimal(item.get("notional_usdc"), item.get("amount"), default=Decimal("0"))
        if notional_usdc == Decimal("0") and size is not None and price is not None:
            notional_usdc = price * size
        yield Fill(
            trace_id=extract_trace_id(item),
            event_id=extract_event_id(item),
            market_slug=extract_market_slug(item),
            condition_id=condition_id,
            token_id=token_id,
            reason=text_value(item.get("reason")) or status,
            created_at=datetime_value(item.get("created_at") or item.get("timestamp")) or datetime.now(timezone.utc),
            order_id=text_value(item.get("order_id") or item.get("taker_order_id")),
            trade_id=text_value(item.get("trade_id") or item.get("matched_trade_id") or item.get("id")),
            side=side_text,
            price=price,
            size=size,
            notional_usdc=notional_usdc,
            status=status,
            confirmed_at=datetime_value(
                item.get("confirmed_at")
                or item.get("matchtime")
                or item.get("last_update")
                or item.get("timestamp")
            ),
        )


def order_from_fill(fill: Fill) -> Order:
    side = OrderSide.BUY if normalized_status(fill.side) == "buy" else OrderSide.SELL
    order_status = OrderStatus.MATCHED
    if fill.status in {"partial", "partially_filled", "partial_fill"}:
        order_status = OrderStatus.PARTIALLY_FILLED
    elif fill.status in {"confirmed", "mined"}:
        order_status = OrderStatus.MATCHED
    elif fill.status in {"failed"}:
        order_status = OrderStatus.FAILED
    return Order(
        trace_id=fill.trace_id,
        condition_id=fill.condition_id or "",
        token_id=fill.token_id or "",
        market_slug=fill.market_slug,
        side=side,
        order_type=OrderType.FAK if side == OrderSide.BUY else OrderType.GTC,
        price=fill.price or Decimal("0"),
        amount_usdc=fill.notional_usdc if side == OrderSide.BUY else None,
        size_shares=fill.size if side == OrderSide.SELL else None,
        notional_usdc=fill.notional_usdc,
        order_id=fill.order_id,
        trade_id=fill.trade_id,
        status=order_status,
        reason=fill.reason,
    )


def apply_fill_to_position(position: Position | None, fill: Fill) -> Position | None:
    if fill.size is None:
        return position

    if position is None:
        if fill.condition_id is None or fill.token_id is None:
            return None
        position = Position(
            condition_id=fill.condition_id,
            token_id=fill.token_id,
            market_slug=fill.market_slug,
            shares=Decimal("0"),
            cost_usdc=Decimal("0"),
            open_buy_shares=Decimal("0"),
            open_sell_shares=Decimal("0"),
            pending_buy_shares=Decimal("0"),
            confirmed_shares=Decimal("0"),
            confirmation_status="unknown",
            updated_at=fill.confirmed_at or fill.created_at,
        )

    size = fill.size
    confirmed = normalized_status(fill.status) in {"confirmed", "mined"}
    side = normalized_status(fill.side)
    updated = position

    if side == "buy":
        return replace(
            updated,
            shares=updated.shares + size,
            cost_usdc=updated.cost_usdc + (fill.notional_usdc or Decimal("0")),
            pending_buy_shares=updated.pending_buy_shares + size if not confirmed else max(
                Decimal("0"),
                updated.pending_buy_shares - size,
            ),
            confirmed_shares=updated.confirmed_shares + size if confirmed else updated.confirmed_shares,
            confirmation_status=normalized_status(fill.status) or updated.confirmation_status,
            last_order_id=fill.order_id or updated.last_order_id,
            last_trade_id=fill.trade_id or updated.last_trade_id,
            updated_at=fill.confirmed_at or fill.created_at,
        )

    if side == "sell":
        return replace(
            updated,
            shares=max(Decimal("0"), updated.shares - size),
            open_sell_shares=max(Decimal("0"), updated.open_sell_shares - size),
            confirmed_shares=max(
                Decimal("0"),
                updated.confirmed_shares - size,
            )
            if confirmed
            else updated.confirmed_shares,
            confirmation_status=normalized_status(fill.status) or updated.confirmation_status,
            last_order_id=fill.order_id or updated.last_order_id,
            last_trade_id=fill.trade_id or updated.last_trade_id,
            updated_at=fill.confirmed_at or fill.created_at,
        )

    return position
