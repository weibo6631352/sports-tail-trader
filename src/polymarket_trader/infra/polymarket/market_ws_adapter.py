from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from polymarket_trader.domain.orderbook import PriceLevel

_FEE_RATE_DENOMINATOR = Decimal("1000")


def decimal_value(value: Any | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    return Decimal(text)


def first_value(mapping: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def nested_mapping(mapping: Mapping[str, Any], *keys: str) -> Mapping[str, Any] | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, Mapping):
            return value
    return None


def bool_value(value: Any | None) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


def bps_value(value: Any | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = Decimal(text)
    except InvalidOperation:
        return None
    if numeric == numeric.to_integral_value():
        return int(numeric)
    if abs(numeric) < Decimal("1"):
        return int((numeric * Decimal("10000")).to_integral_value())
    return int(numeric.to_integral_value())


def fee_rate_units(value: Any | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = Decimal(text)
    except InvalidOperation:
        return None
    if numeric < Decimal("0"):
        return None
    if numeric < Decimal("1"):
        return int((numeric * _FEE_RATE_DENOMINATOR).to_integral_value())
    if numeric == numeric.to_integral_value():
        return int(numeric)
    return int((numeric * _FEE_RATE_DENOMINATOR).to_integral_value())


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


def extract_token_id(message: Mapping[str, Any]) -> str | None:
    value = first_value(
        message,
        "token_id",
        "tokenId",
        "asset_id",
        "assetId",
        "market_token_id",
        "winning_asset_id",
    )
    return None if value is None else str(value)


def extract_sequence(message: Mapping[str, Any]) -> int | None:
    value = first_value(message, "sequence", "seq", "version")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_levels(value: Any) -> tuple[PriceLevel, ...]:
    if not isinstance(value, list):
        return ()
    levels: list[PriceLevel] = []
    for item in value:
        if isinstance(item, Mapping):
            price = decimal_value(first_value(item, "price", "p"))
            size = decimal_value(first_value(item, "size", "quantity", "qty", "amount"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price = decimal_value(item[0])
            size = decimal_value(item[1])
        else:
            continue
        if price is None or size is None:
            continue
        levels.append(PriceLevel(price=price, size=size))
    return tuple(levels)


def message_type(message: Mapping[str, Any]) -> str:
    value = first_value(message, "event_type", "message_type", "channel_event", "event", "type", "action")
    return "" if value is None else str(value).strip().lower()


def extract_token_candidates(message: Mapping[str, Any]) -> tuple[str, ...]:
    candidates: list[str] = []
    seen: set[str] = set()
    direct = extract_token_id(message)
    if direct is not None and direct not in seen:
        candidates.append(direct)
        seen.add(direct)
    for key in ("assets_ids", "clob_token_ids"):
        value = message.get(key)
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            text = str(item).strip()
            if text and text not in seen:
                candidates.append(text)
                seen.add(text)
    return tuple(candidates)


def best_price(levels: tuple[PriceLevel, ...]) -> Decimal | None:
    if not levels:
        return None
    return max(level.price for level in levels)


def worst_ask(levels: tuple[PriceLevel, ...]) -> Decimal | None:
    if not levels:
        return None
    return min(level.price for level in levels)
