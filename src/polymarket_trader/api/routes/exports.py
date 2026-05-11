"""Streaming export endpoint.

只读端点，按时间窗口流式导出 orders / fills / audit_events 三类审计快照表，
不调用任何交易客户端、不触发风控决策、不写库；走 stream_scalars 的 server-side
cursor，避免一次性把全表加载进内存阻塞 P0 路径。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_trader.api.deps import build_time_range
from polymarket_trader.api.rate_limit import rate_limit
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import AuditEventModel, FillModel, OrderModel
from polymarket_trader.infra.db.repositories import (
    AuditEventRepository,
    FillRepository,
    OrderRepository,
)


router = APIRouter(prefix="/exports", tags=["exports"])

_ALLOWED_RESOURCES = ("orders", "fills", "audit_events")
_ALLOWED_FORMATS = ("csv", "jsonl")
DEFAULT_LIMIT = 10_000
MAX_LIMIT = 100_000

_LIMIT_CLAMPED_HEADER = "X-Export-Warning"
_LIMIT_CLAMPED_VALUE = "limit_clamped_to_100000"

_RESOURCE_MODELS: dict[str, type[Any]] = {
    "orders": OrderModel,
    "fills": FillModel,
    "audit_events": AuditEventModel,
}


def _columns_for(resource: str) -> tuple[str, ...]:
    model = _RESOURCE_MODELS[resource]
    return tuple(column.name for column in model.__table__.columns)


def _resolve_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """优先取 app.state 注入的 session_factory；其次回退到 runtime。

    与其他 admin 端点共享 503 语义，session_factory 不存在时不返回半截响应。
    """

    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        runtime = getattr(request.app.state, "runtime", None)
        factory = getattr(runtime, "db_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="db_session_factory_unavailable")
    return factory


async def _stream_orders(
    session: AsyncSession, time_range: TimeRange | None, limit: int
) -> AsyncIterator[OrderModel]:
    repo = OrderRepository(session)
    async for row in repo.stream_orders_in_range(time_range=time_range, limit=limit):
        yield row


async def _stream_fills(
    session: AsyncSession, time_range: TimeRange | None, limit: int
) -> AsyncIterator[FillModel]:
    repo = FillRepository(session)
    async for row in repo.stream_fills_in_range(time_range=time_range, limit=limit):
        yield row


async def _stream_audit_events(
    session: AsyncSession, time_range: TimeRange | None, limit: int
) -> AsyncIterator[AuditEventModel]:
    repo = AuditEventRepository(session)
    async for row in repo.stream_audit_events_in_range(time_range=time_range, limit=limit):
        yield row


_STREAM_FN_BY_RESOURCE: dict[
    str,
    Callable[
        [AsyncSession, TimeRange | None, int],
        AsyncIterator[Any],
    ],
] = {
    "orders": _stream_orders,
    "fills": _stream_fills,
    "audit_events": _stream_audit_events,
}


def _row_value(row: Any, column: str) -> Any:
    return getattr(row, column, None)


def _format_cell_csv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=_json_default)
    return str(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (set, frozenset)):
        return list(value)
    return str(value)


def _row_to_jsonl_object(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    return {column: _row_value(row, column) for column in columns}


async def _csv_stream(
    rows: AsyncIterator[Any], columns: tuple[str, ...]
) -> AsyncIterator[bytes]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(columns))
    yield buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)
    async for row in rows:
        writer.writerow([_format_cell_csv(_row_value(row, column)) for column in columns])
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)


async def _jsonl_stream(
    rows: AsyncIterator[Any], columns: tuple[str, ...]
) -> AsyncIterator[bytes]:
    async for row in rows:
        obj = _row_to_jsonl_object(row, columns)
        yield (json.dumps(obj, ensure_ascii=False, default=_json_default) + "\n").encode("utf-8")


@router.get("/{resource}")
async def export_resource(
    resource: str,
    request: Request,
    format: str = Query(..., description="csv | jsonl"),
    since: int | None = Query(default=None, ge=0, description="epoch_ms inclusive lower bound"),
    until: int | None = Query(default=None, ge=0, description="epoch_ms inclusive upper bound"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, description="max rows; capped at 100000"),
    _rate: None = Depends(rate_limit(endpoint="export_resource", qps=0.2, burst=1)),
) -> StreamingResponse:
    if resource not in _ALLOWED_RESOURCES:
        raise HTTPException(
            status_code=404,
            detail=f"unknown_resource: {resource}; allowed={list(_ALLOWED_RESOURCES)}",
        )
    if format not in _ALLOWED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported_format: {format}; allowed={list(_ALLOWED_FORMATS)}",
        )

    time_range = build_time_range(since=since, until=until)
    clamped_limit = min(limit, MAX_LIMIT)
    session_factory = _resolve_session_factory(request)
    columns = _columns_for(resource)
    stream_fn = _STREAM_FN_BY_RESOURCE[resource]

    if format == "csv":
        media_type = "text/csv; charset=utf-8"
        extension = "csv"
    else:
        media_type = "application/x-ndjson"
        extension = "jsonl"

    async def _row_iterator() -> AsyncIterator[Any]:
        async with session_factory() as session:
            async for row in stream_fn(session, time_range, clamped_limit):
                yield row

    body: AsyncIterator[bytes]
    if format == "csv":
        body = _csv_stream(_row_iterator(), columns)
    else:
        body = _jsonl_stream(_row_iterator(), columns)

    since_part = "" if since is None else str(since)
    until_part = "" if until is None else str(until)
    filename = f"{resource}_{since_part}_{until_part}.{extension}"
    headers: dict[str, str] = {
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    if limit > MAX_LIMIT:
        headers[_LIMIT_CLAMPED_HEADER] = _LIMIT_CLAMPED_VALUE

    return StreamingResponse(body, media_type=media_type, headers=headers)
