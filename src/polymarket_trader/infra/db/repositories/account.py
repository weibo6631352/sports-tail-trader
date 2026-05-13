from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polymarket_trader.domain.account import AccountHistoryPoint, AccountSnapshot
from polymarket_trader.infra.db.models import AccountSnapshotModel, _decimal, _ensure_aware
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    _row_dict,
)


class AccountSnapshotRepository(BaseRepository):
    """账户余额快照仓储——append-only 时间序列。

    每次 ``save_snapshot`` 都插入一行新记录。读取"当前账户状态"必须按
    ``recorded_at DESC LIMIT 1`` 查最新一行；``query_history_bucketed`` 用
    ``date_trunc`` 做服务端 downsampling，避免把原始点全部拉到 Python。
    """

    async def save_snapshot(
        self,
        snapshot: AccountSnapshot,
        *,
        trace_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
        account_key: str = "primary",
        recorded_at: datetime | None = None,
        net_value_usdc: Decimal | None = None,
    ) -> AccountSnapshot:
        await self.save_snapshots(
            [snapshot],
            trace_id=trace_id,
            raw_payloads=[raw_payload],
            account_key=account_key,
            recorded_at=recorded_at,
            net_values_usdc=None if net_value_usdc is None else [net_value_usdc],
        )
        return snapshot

    async def save_snapshots(
        self,
        snapshots: Iterable[AccountSnapshot],
        *,
        trace_id: str | None = None,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
        account_key: str = "primary",
        recorded_at: datetime | None = None,
        net_values_usdc: Sequence[Decimal | None] | None = None,
    ) -> int:
        snapshots = tuple(snapshots)
        if not snapshots:
            return 0
        payloads = raw_payloads or (None,) * len(snapshots)
        net_values = net_values_usdc or (None,) * len(snapshots)
        rows = [
            _row_dict(
                AccountSnapshotModel.from_domain(
                    snapshot,
                    trace_id=trace_id,
                    raw_payload=payload,
                    account_key=account_key,
                    recorded_at=recorded_at,
                    net_value_usdc=net_value,
                )
            )
            for snapshot, payload, net_value in zip(
                snapshots, payloads, net_values, strict=False
            )
        ]
        # append-only：直接 INSERT，没有 ON CONFLICT 收敛。
        stmt = pg_insert(AccountSnapshotModel).values(list(rows))
        await self._session.execute(stmt)
        return len(rows)

    async def get_current_snapshot(self, *, account_key: str = "primary") -> AccountSnapshot | None:
        stmt = (
            select(AccountSnapshotModel)
            .where(AccountSnapshotModel.account_key == account_key)
            .order_by(AccountSnapshotModel.recorded_at.desc())
            .limit(1)
        )
        row = await self._session.scalar(stmt)
        return None if row is None else row.to_domain()

    async def query_history_bucketed(
        self,
        *,
        since: datetime,
        until: datetime,
        interval_ms: int,
        account_key: str = "primary",
    ) -> tuple[AccountHistoryPoint, ...]:
        """按 ``interval_ms`` 服务器端 downsampling 净值时间序列。

        实现：用 ``to_timestamp(floor(epoch / interval_s) * interval_s)`` 计算
        bucket，每个 bucket 内取最新一行。所有聚合在 PG 内完成，Python 端只拿
        ``≈ window/interval`` 条结果。
        """

        if interval_ms <= 0:
            raise ValueError("interval_ms must be > 0")
        since_aware = _ensure_aware(since)
        until_aware = _ensure_aware(until)
        interval_s = interval_ms / 1000.0
        bucket_expr = func.to_timestamp(
            func.floor(func.extract("epoch", AccountSnapshotModel.recorded_at) / interval_s)
            * interval_s
        ).label("bucket_start")
        # DISTINCT ON bucket：每个 bucket 取 recorded_at 最大那一行。
        stmt = (
            select(
                bucket_expr,
                AccountSnapshotModel.recorded_at,
                AccountSnapshotModel.net_value_usdc,
            )
            .where(AccountSnapshotModel.account_key == account_key)
            .where(AccountSnapshotModel.recorded_at >= since_aware)
            .where(AccountSnapshotModel.recorded_at <= until_aware)
            .distinct(bucket_expr)
            .order_by(bucket_expr, AccountSnapshotModel.recorded_at.desc())
        )
        result = await self._session.execute(stmt)
        points: list[AccountHistoryPoint] = []
        for _bucket_start, recorded_at, net_value in result.all():
            points.append(
                AccountHistoryPoint(
                    recorded_at=_ensure_aware(recorded_at),
                    net_value_usdc=_decimal(net_value) or Decimal("0"),
                )
            )
        points.sort(key=lambda point: point.recorded_at)
        return tuple(points)


__all__ = ["AccountHistoryPoint", "AccountSnapshotRepository"]
