"""Polymarket schemas 包内通用解析工具。

只做"原始 payload → 基础类型 / domain 兼容值"的转换。所有 DTO 子模块都从这里
引用 helper；不依赖任何 DTO，避免子模块之间的循环依赖。
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from json import loads
from typing import Any

from polymarket_trader.domain.market import MarketOutcome
from polymarket_trader.domain.orderbook import PriceLevel

_FEE_RATE_DENOMINATOR = Decimal("1000")
_USDC_BASE_UNITS = Decimal("1000000")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _first_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _first_value(payload: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _maybe_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _iter_items(payload: Any, *keys: str) -> tuple[Any, ...]:
    if isinstance(payload, list):
        return tuple(payload)
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return tuple(value)
    return ()


def _iter_mappings(payload: Any, *keys: str) -> tuple[Mapping[str, Any], ...]:
    items = _iter_items(payload, *keys)
    mappings: list[Mapping[str, Any]] = []
    for item in items:
        maybe = _maybe_mapping(item)
        if maybe is not None:
            mappings.append(maybe)
    return tuple(mappings)


def _unwrap_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    current: Mapping[str, Any] = payload
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        for key in ("data", "payload", "result", "order", "orderbook", "book"):
            value = current.get(key)
            if isinstance(value, Mapping):
                current = value
                break
        else:
            return current
    return payload


def _coerce_decimal(value: Any | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    with contextlib.suppress(InvalidOperation):
        return Decimal(text)
    return None


def _coerce_collateral_usdc(value: Any | None) -> Decimal | None:
    amount = _coerce_decimal(value)
    if amount is None:
        return None
    return amount / _USDC_BASE_UNITS


def _coerce_allowance_usdc(payload: Mapping[str, Any]) -> Decimal | None:
    allowance = _coerce_collateral_usdc(_first_value(payload, "allowance"))
    if allowance is not None:
        return allowance
    allowances = payload.get("allowances")
    if not isinstance(allowances, Mapping):
        return None
    values = [
        value
        for value in (_coerce_collateral_usdc(raw_value) for raw_value in allowances.values())
        if value is not None
    ]
    if not values:
        return None
    return max(values)


def _coerce_int(value: Any | None) -> int | None:
    number = _coerce_decimal(value)
    if number is None:
        return None
    with contextlib.suppress(ArithmeticError, ValueError):
        return int(number)
    return None


def _coerce_fee_rate_units(value: Any | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    numeric = _coerce_decimal(value)
    if numeric is None or numeric < Decimal("0"):
        return None
    if numeric < Decimal("1"):
        return int((numeric * _FEE_RATE_DENOMINATOR).to_integral_value())
    if numeric == numeric.to_integral_value():
        return int(numeric)
    return int((numeric * _FEE_RATE_DENOMINATOR).to_integral_value())


def _coerce_bool(value: Any | None) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"1", "true", "t", "yes", "y", "enabled", "active", "open"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "closed"}:
        return False
    return None


def _coerce_datetime(value: Any | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        with contextlib.suppress(ValueError, OSError, OverflowError):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        with contextlib.suppress(ValueError, OSError, OverflowError):
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _string_tuple(value: Any | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value_text = value.strip()
        if value_text and value_text[0] in '[{"':
            with contextlib.suppress(TypeError, ValueError):
                return _string_tuple(loads(value_text))
        return (value_text,) if value_text else ()
    if isinstance(value, Mapping):
        mapping_items: list[str] = []
        for key in ("label", "slug", "name"):
            key_text = _first_text(value, key)
            if key_text:
                mapping_items.append(key_text)
        return tuple(mapping_items)
    if isinstance(value, (list, tuple, set, frozenset)):
        nested_items: list[str] = []
        for item in value:
            nested_items.extend(_string_tuple(item))
        return tuple(nested_items)
    value_text = str(value).strip()
    return (value_text,) if value_text else ()


def _token_id_tuple(value: Any | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        with contextlib.suppress(TypeError, ValueError):
            parsed = loads(text)
            return _token_id_tuple(parsed)
        return (text,)
    if isinstance(value, (list, tuple, set, frozenset)):
        items: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                items.append(text)
        return tuple(items)
    text = str(value).strip()
    return (text,) if text else ()


def _market_outcomes(
    token_ids: tuple[str, ...],
    outcome_names: tuple[str, ...],
) -> tuple[MarketOutcome, ...]:
    if not token_ids:
        return ()
    resolved_names = list(outcome_names)
    if len(resolved_names) < len(token_ids):
        if not resolved_names and len(token_ids) == 2:
            resolved_names = ["YES", "NO"]
        while len(resolved_names) < len(token_ids):
            resolved_names.append(f"OUTCOME_{len(resolved_names)}")
    return tuple(
        MarketOutcome(token_id=token_id, outcome=resolved_names[index])
        for index, token_id in enumerate(token_ids)
    )


def _coerce_price_levels(value: Any) -> tuple[PriceLevel, ...]:
    if not isinstance(value, list):
        return ()
    levels: list[PriceLevel] = []
    for item in value:
        if isinstance(item, Mapping):
            price = _coerce_decimal(_first_value(item, "price", "p"))
            size = _coerce_decimal(_first_value(item, "size", "qty", "quantity", "amount"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price = _coerce_decimal(item[0])
            size = _coerce_decimal(item[1])
        else:
            continue
        if price is None or size is None:
            continue
        levels.append(PriceLevel(price=price, size=size))
    return tuple(levels)


def _normalize_address(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    return text if text.startswith("0x") else None
