from __future__ import annotations

from json import dumps, loads
from typing import Any, Iterable, Mapping


def normalize_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return payload


def extract_market_payloads(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    markets = payload.get("markets")
    if isinstance(markets, list):
        for item in markets:
            maybe = maybe_mapping(item)
            if maybe is not None:
                yield maybe
        return

    events = payload.get("events")
    if isinstance(events, list):
        for item in events:
            maybe = maybe_mapping(item)
            if maybe is None:
                continue
            nested_markets = maybe.get("markets")
            if isinstance(nested_markets, list):
                for market in nested_markets:
                    nested = maybe_mapping(market)
                    if nested is not None:
                        yield nested
                continue
            nested_market = maybe.get("market")
            if isinstance(nested_market, Mapping):
                yield nested_market
                continue
            if maybe.get("condition_id") or maybe.get("market_slug") or maybe.get("slug"):
                yield maybe
        return

    if payload.get("condition_id") or payload.get("market_slug") or payload.get("slug"):
        yield payload


def payload_signature(payload: Mapping[str, Any]) -> str:
    stable = {
        "condition_id": first_text(payload, "condition_id", "conditionId", "condition"),
        "market_slug": first_text(payload, "market_slug", "marketSlug", "slug"),
        "market_name": first_text(payload, "title", "name", "market_name"),
        "question": first_text(payload, "question", "prompt", "market_question"),
        "event_id": first_text(payload, "event_id", "eventId", "id"),
        "event_slug": first_text(payload, "event_slug", "eventSlug"),
        "event_title": first_text(payload, "event_title", "eventTitle", "title", "name"),
        "category": first_text(payload, "category", "cat"),
        "tags": normalize_tags(first_value(payload, "tags")),
        "token_ids": normalize_token_ids(first_value(payload, "clobTokenIds", "clob_token_ids")),
        "tick_size": first_text(payload, "orderPriceMinTickSize", "tick_size", "tickSize"),
        "min_order_size": first_text(payload, "orderMinSize", "min_order_size", "minOrderSize"),
        "active": first_value(payload, "active", "is_active"),
        "closed": first_value(payload, "closed", "is_closed"),
        "archived": first_value(payload, "archived", "is_archived"),
        "clob_enabled": first_value(
            payload,
            "clob_enabled",
            "enableOrderBook",
            "clobEnabled",
            "acceptingOrders",
        ),
        "fees_enabled": first_value(payload, "fees_enabled", "feesEnabled"),
        "maker_base_fee_bps": first_value(
            payload,
            "maker_base_fee_bps",
            "makerBaseFee",
            "maker_base_fee",
        ),
        "taker_base_fee_bps": first_value(
            payload,
            "taker_base_fee_bps",
            "takerBaseFee",
            "taker_base_fee",
        ),
        "fee_schedule": normalize_fee_schedule(first_value(payload, "feeSchedule", "fee_schedule")),
        "end_date": first_text(payload, "endDate", "end_date", "endDateIso"),
        "game_start_time": first_text(
            payload,
            "gameStartTime",
            "game_start_time",
            "gameStart",
        ),
        "icon_url": first_text(payload, "icon"),
    }
    return dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def maybe_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def first_value(payload: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def first_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    value = first_value(payload, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value_text = value.strip()
        return (value_text,) if value_text else ()
    if isinstance(value, Mapping):
        mapping_tags: list[str] = []
        for key in ("label", "slug", "name"):
            key_text = first_text(value, key)
            if key_text:
                mapping_tags.append(key_text)
        return tuple(mapping_tags)
    if isinstance(value, (list, tuple, set, frozenset)):
        nested_tags: list[str] = []
        for item in value:
            nested_tags.extend(normalize_tags(item))
        return tuple(nested_tags)
    value_text = str(value).strip()
    return (value_text,) if value_text else ()


def normalize_token_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = loads(text)
            except ValueError:
                return (text,)
            return normalize_token_ids(parsed)
        return (text,)
    if isinstance(value, (list, tuple, set, frozenset)):
        token_ids: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                token_ids.append(text)
        return tuple(token_ids)
    text = str(value).strip()
    return (text,) if text else ()


def normalize_fee_schedule(value: Any) -> Mapping[str, Any] | None:
    schedule = maybe_mapping(value)
    if schedule is None:
        return None
    return {
        "enabled": first_value(schedule, "enabled", "feesEnabled"),
        "rate": first_value(schedule, "rate", "base_fee", "baseFee"),
        "exponent": first_value(schedule, "exponent"),
        "taker_only": first_value(schedule, "takerOnly", "taker_only"),
        "rebate_rate": first_value(schedule, "rebateRate", "rebate_rate"),
    }
