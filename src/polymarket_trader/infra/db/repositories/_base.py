from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket_trader.infra.db.base import Base

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RepositoryPage(Generic[T]):
    items: tuple[T, ...]
    total: int
    limit: int
    offset: int


class BaseRepository:
    """仓储基类。

    仓储只负责 PostgreSQL 写读和幂等 upsert，不承担风控、决策和交易编排。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _bulk_upsert(
        self,
        model: type[Base],
        rows: Sequence[dict[str, Any]],
        *,
        conflict_columns: Sequence[str],
        update_columns: Sequence[str],
    ) -> int:
        if not rows:
            return 0
        stmt = pg_insert(model).values(list(rows))
        if update_columns:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(conflict_columns),
                set_={column: getattr(stmt.excluded, column) for column in update_columns},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_columns))
        await self._session.execute(stmt)
        return len(rows)

    async def _paginate(
        self,
        stmt: Select[Any],
        *,
        limit: int,
        offset: int,
        with_total: bool = True,
    ) -> tuple[list[Any], int]:
        if with_total:
            base_stmt = stmt.order_by(None)
            total_stmt = select(func.count()).select_from(base_stmt.subquery())
            total = int((await self._session.scalar(total_stmt)) or 0)
        else:
            total = -1  # sentinel：调用方不需要 total，跳过 count subquery
        result = await self._session.scalars(stmt.limit(limit).offset(offset))
        return list(result.all()), total


def _limit_offset(limit: int, offset: int) -> tuple[int, int]:
    if limit <= 0:
        limit = 100
    if offset < 0:
        offset = 0
    return limit, offset


def _row_dict(model: Any) -> dict[str, Any]:
    return {key: value for key, value in vars(model).items() if not key.startswith("_sa_")}


__all__ = [
    "BaseRepository",
    "RepositoryPage",
    "T",
    "_limit_offset",
    "_row_dict",
]
