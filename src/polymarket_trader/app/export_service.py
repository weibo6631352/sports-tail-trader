"""Export streaming service: moves ORM/repository access out of the API layer.

API routes depend only on this service — no ORM model classes or repository
imports in the routes themselves.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import AuditEventModel, FillModel, OrderModel
from polymarket_trader.infra.db.repositories import (
    AuditEventRepository,
    FillRepository,
    OrderRepository,
)

_RESOURCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "orders": tuple(col.name for col in OrderModel.__table__.columns),
    "fills": tuple(col.name for col in FillModel.__table__.columns),
    "audit_events": tuple(col.name for col in AuditEventModel.__table__.columns),
}

ALLOWED_RESOURCES: tuple[str, ...] = tuple(_RESOURCE_COLUMNS)


def columns_for_resource(resource: str) -> tuple[str, ...]:
    return _RESOURCE_COLUMNS[resource]


async def stream_resource_rows(
    session: AsyncSession,
    resource: str,
    time_range: TimeRange | None,
    limit: int,
) -> AsyncIterator[Any]:
    """Yield raw ORM model rows for the given resource."""
    if resource == "orders":
        async for row in OrderRepository(session).stream_orders_in_range(
            time_range=time_range, limit=limit
        ):
            yield row
    elif resource == "fills":
        async for row in FillRepository(session).stream_fills_in_range(
            time_range=time_range, limit=limit
        ):
            yield row
    elif resource == "audit_events":
        async for row in AuditEventRepository(session).stream_audit_events_in_range(
            time_range=time_range, limit=limit
        ):
            yield row
