from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import select

from polymarket_trader.domain.position import Position
from polymarket_trader.infra.db.models import PositionModel
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
    _limit_offset,
    _row_dict,
)


class PositionRepository(BaseRepository):
    """持仓仓储。"""

    async def save_position(
        self,
        position: Position,
        *,
        trace_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> Position:
        await self.save_positions([position], trace_id=trace_id, raw_payloads=[raw_payload])
        return position

    async def save_positions(
        self,
        positions: Iterable[Position],
        *,
        trace_id: str | None = None,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        positions = tuple(positions)
        payloads = raw_payloads or (None,) * len(positions)
        rows = [
            _row_dict(PositionModel.from_domain(position, trace_id=trace_id, raw_payload=payload))
            for position, payload in zip(positions, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            PositionModel,
            rows,
            conflict_columns=("position_key",),
            update_columns=(
                "trace_id",
                "condition_id",
                "token_id",
                "market_slug",
                "shares",
                "cost_usdc",
                "open_buy_shares",
                "open_sell_shares",
                "pending_buy_shares",
                "confirmed_shares",
                "last_order_id",
                "last_trade_id",
                "confirmation_status",
                "avg_price",
                "initial_value",
                "current_value",
                "cash_pnl",
                "percent_pnl",
                "realized_pnl",
                "percent_realized_pnl",
                "cur_price",
                "redeemable",
                "raw_payload",
                "updated_at",
            ),
        )

    async def list_positions_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> RepositoryPage[Position]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(PositionModel).order_by(PositionModel.updated_at.desc(), PositionModel.id.desc())
        if condition_id is not None:
            stmt = stmt.where(PositionModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(PositionModel.token_id == token_id)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)

    async def list_by_condition_ids(
        self,
        condition_ids: Sequence[str],
    ) -> tuple[Position, ...]:
        """按 ``condition_id`` 集合批量取仓位——给跨表聚合（edge-realization 等）用，
        避免 N+1。无 condition_ids 时返回空 tuple。"""

        ids = tuple({cid for cid in condition_ids if cid})
        if not ids:
            return ()
        stmt = select(PositionModel).where(PositionModel.condition_id.in_(ids))
        result = await self._session.scalars(stmt)
        return tuple(row.to_domain() for row in result.all())


__all__ = ["PositionRepository"]
