from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from polymarket_trader.infra.db import RepositoryPage


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def decimal_text(value: Any | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return str(value)


def page_payload(
    page: "RepositoryPage[Any]", *, serializer: Callable[[Any], Any]
) -> dict[str, Any]:
    """RepositoryPage → 统一 operator/aggregator 分页响应形态。

    total = -1 是 sentinel：调用方传 with_total=False 跳过 count subquery，
    此时对外返回 None 让前端区分"未计数"vs"0 条"。
    """

    return {
        "items": [serializer(item) for item in page.items],
        "total": page.total if page.total >= 0 else None,
        "limit": page.limit,
        "offset": page.offset,
    }
