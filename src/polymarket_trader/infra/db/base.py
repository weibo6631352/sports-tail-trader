from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from polymarket_trader.domain.account import MarketPause
from polymarket_trader.domain.order import Order, OrderResult
from polymarket_trader.domain.orderbook import PriceLevel

JsonValue = Any
JsonMapping = Mapping[str, Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decimal(value: Any | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    return Decimal(text_value)


def _text(value: Any | None) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _db_key(value: Any | None, *, limit: int = 255) -> str | None:
    """将本地长幂等键稳定压缩到数据库索引字段长度内。"""

    text_value = _text(value)
    if text_value is None:
        return None
    if len(text_value) <= limit:
        return text_value
    digest = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
    suffix = f":sha256:{digest}"
    return f"{text_value[: limit - len(suffix)]}{suffix}"


def _datetime_value(value: Any | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return _ensure_aware(datetime.fromisoformat(text_value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _fee_rate_units_from_payload(payload: JsonMapping | None) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    fee_schedule = payload.get("feeSchedule")
    if not isinstance(fee_schedule, Mapping):
        fee_schedule = payload.get("fee_schedule")
    if not isinstance(fee_schedule, Mapping):
        return None
    rate = fee_schedule.get("rate")
    if rate is None:
        rate = fee_schedule.get("base_fee")
    if rate is None:
        rate = fee_schedule.get("baseFee")
    if rate is None or isinstance(rate, bool):
        return None
    numeric = _decimal(rate)
    if numeric is None or numeric < Decimal("0"):
        return None
    if numeric < Decimal("1"):
        return int((numeric * Decimal("1000")).to_integral_value())
    if numeric == numeric.to_integral_value():
        return int(numeric)
    return int((numeric * Decimal("1000")).to_integral_value())


def _json_safe(value: Any) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:
        return str(value)


def _json_mapping(value: JsonMapping | None) -> dict[str, Any]:
    if not value:
        return {}
    return {str(key): _json_safe(item) for key, item in value.items()}


def _json_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _tuple_from_sequence(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in value if str(item).strip())
    text_value = str(value).strip()
    return (text_value,) if text_value else ()


def _market_pause_payloads(pauses: tuple[MarketPause, ...]) -> list[dict[str, JsonValue]]:
    return [_json_safe(pause.as_payload()) for pause in pauses]


def _market_pauses_from_payload(value: Any) -> tuple[MarketPause, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    pauses: list[MarketPause] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        condition_id = _text(item.get("condition_id"))
        reason = _text(item.get("reason"))
        source = _text(item.get("source"))
        recoverable = item.get("recoverable")
        if condition_id is None or reason is None or source is None or not isinstance(recoverable, bool):
            continue
        try:
            pauses.append(
                MarketPause.build(
                    condition_id=condition_id,
                    reason=reason,
                    source=source,
                    recoverable=recoverable,
                )
            )
        except ValueError:
            continue
    return tuple(pauses)


def _level_to_json(level: PriceLevel) -> dict[str, str]:
    return {"price": str(level.price), "size": str(level.size)}


def _level_from_json(value: Any) -> PriceLevel:
    if isinstance(value, Mapping):
        return PriceLevel(price=Decimal(str(value["price"])), size=Decimal(str(value["size"])))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return PriceLevel(price=Decimal(str(value[0])), size=Decimal(str(value[1])))
    raise TypeError(f"unsupported price level payload: {value!r}")


def _order_key(order: Order | OrderResult) -> str:
    if isinstance(order, Order):
        if order.order_id:
            return order.order_id
        if order.idempotency_key:
            return _db_key(order.idempotency_key) or ""
        return _db_key("|".join(
            [
                order.trace_id or "",
                order.condition_id,
                order.token_id,
                order.side.value,
                order.order_type.value,
            ]
        )) or ""
    if order.order_id:
        return order.order_id
    if order.intent is not None and getattr(order.intent, "idempotency_key", None):
        return _db_key(order.intent.idempotency_key) or ""
    return _db_key("|".join(
        [
            order.trace_id,
            order.condition_id,
            order.token_id,
            "" if order.side is None else order.side.value,
            "" if order.order_type is None else order.order_type.value,
        ]
    )) or ""


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = [
    "Base",
    "JsonMapping",
    "JsonValue",
    "TimestampMixin",
    "_datetime_value",
    "_db_key",
    "_decimal",
    "_ensure_aware",
    "_fee_rate_units_from_payload",
    "_json_bool",
    "_json_mapping",
    "_json_safe",
    "_level_from_json",
    "_level_to_json",
    "_market_pause_payloads",
    "_market_pauses_from_payload",
    "_order_key",
    "_text",
    "_tuple_from_sequence",
    "_utc_now",
]
