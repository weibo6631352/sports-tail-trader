from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import select

from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import OrderbookSnapshotModel
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
    _limit_offset,
    _row_dict,
)


class OrderbookSnapshotRepository(BaseRepository):
    """orderbook 快照仓储。"""

    async def save_snapshot(
        self,
        snapshot: OrderbookSnapshot,
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> OrderbookSnapshot:
        await self.save_snapshots([snapshot], trace_id=trace_id, source=source, raw_payloads=[raw_payload])
        return snapshot

    async def save_snapshots(
        self,
        snapshots: Iterable[OrderbookSnapshot],
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        snapshots = tuple(snapshots)
        payloads = raw_payloads or (None,) * len(snapshots)
        rows = [
            _row_dict(
                OrderbookSnapshotModel.from_domain(
                    snapshot,
                    trace_id=trace_id,
                    source=source,
                    raw_payload=payload,
                )
            )
            for snapshot, payload in zip(snapshots, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            OrderbookSnapshotModel,
            rows,
            conflict_columns=("snapshot_key",),
            update_columns=(
                "trace_id",
                "source",
                "token_id",
                "condition_id",
                "market_slug",
                "received_at",
                "best_bid",
                "best_ask",
                "best_bid_size",
                "best_ask_size",
                "last_trade_price",
                "tick_size",
                "bids",
                "asks",
                "raw_payload",
                "updated_at",
            ),
        )

    async def list_latest_snapshot(
        self,
        token_id: str,
    ) -> OrderbookSnapshot | None:
        stmt = (
            select(OrderbookSnapshotModel)
            .where(OrderbookSnapshotModel.token_id == token_id)
            .order_by(OrderbookSnapshotModel.received_at.desc(), OrderbookSnapshotModel.id.desc())
            .limit(1)
        )
        row = await self._session.scalar(stmt)
        return None if row is None else row.to_domain()

    async def list_snapshots(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        token_id: str | None = None,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> RepositoryPage[OrderbookSnapshot]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(OrderbookSnapshotModel).order_by(
            OrderbookSnapshotModel.received_at.desc(),
            OrderbookSnapshotModel.id.desc(),
        )
        if token_id is not None:
            stmt = stmt.where(OrderbookSnapshotModel.token_id == token_id)
        if condition_id is not None:
            stmt = stmt.where(OrderbookSnapshotModel.condition_id == condition_id)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(OrderbookSnapshotModel.received_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(OrderbookSnapshotModel.received_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )


__all__ = ["OrderbookSnapshotRepository"]
