"""死记录滚动清理 — 按时间清,各表独立 retention_days。

简单路线(放弃了关联查询):
- ``audit_events`` / ``outbox_events`` / ``decision_records`` / ``account_snapshots`` /
  ``fills``:按各自 ``retention_days`` 清 ``created_at < now() - N days`` 的行。
  索引在 created_at 上有,DELETE 走索引快。
- ``orders``:终态(failed/cancelled/matched/expired)且超 ``dead_records_retention_days`` 清。
  active 状态(live/created/matched 等可变 active)绝不删。
- ``positions``:全字段(shares/open_*/pending)全 0 且 updated_at 超
  ``dead_records_retention_days`` 清 — 链上已结算的空仓。active 永不删。

每张表 ctid 批量 DELETE 控制单次锁表时间。

CLAUDE.md §3"DB 是审计/复盘/恢复参考,不是状态真相";§7"后台任务不能
决定 P0 交易主链路是否继续执行" — 任何 DB 异常只记日志,不抛错。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_trader.serialization import utc_now

logger = logging.getLogger(__name__)


_BATCH_SIZE = 10_000
_MAX_BATCHES = 500  # 单次 job 上限 500 万行,超出留给下一轮 daily 跑


async def _batched_delete(
    session_factory: async_sessionmaker[AsyncSession],
    table: str,
    where_clause: str,
    params: dict[str, Any],
) -> int:
    """ctid-based 批量 DELETE,避免锁全表。单批 _BATCH_SIZE 行,最多 _MAX_BATCHES 轮。"""

    delete_sql = text(
        f"""
        WITH expired AS (
            SELECT ctid FROM {table}
            WHERE {where_clause}
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM expired)
        """
    )
    total = 0
    for _ in range(_MAX_BATCHES):
        async with session_factory() as session:
            result = await session.execute(
                delete_sql, {**params, "batch_size": _BATCH_SIZE}
            )
            deleted = result.rowcount or 0
            await session.commit()
        total += deleted
        if deleted < _BATCH_SIZE:
            break
    return total


async def purge_dead_records_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    dead_records_retention_days: int,
    fills_retention_days: int,
    outbox_retention_days: int,
    decision_records_retention_days: int,
    account_snapshots_retention_days: int,
) -> dict[str, Any]:
    """按时间清理各表死记录,batched DELETE。"""

    started_at = utc_now()
    summary: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "deleted_orders": 0,
        "deleted_fills": 0,
        "deleted_positions": 0,
        "deleted_audit_events": 0,
        "deleted_outbox_events": 0,
        "deleted_decision_records": 0,
        "deleted_account_snapshots": 0,
        "completed_at": None,
        "error": None,
    }

    try:
        # 1. 大表(append-only audit/event)按时间清。
        # audit_events 用既有 audit_retention 配置,这里不重复清,只列出供 dashboard。
        if outbox_retention_days > 0:
            summary["deleted_outbox_events"] = await _batched_delete(
                session_factory, "outbox_events",
                "created_at < :cutoff",
                {"cutoff": started_at - timedelta(days=outbox_retention_days)},
            )
        if decision_records_retention_days > 0:
            summary["deleted_decision_records"] = await _batched_delete(
                session_factory, "decision_records",
                "created_at < :cutoff",
                {"cutoff": started_at - timedelta(days=decision_records_retention_days)},
            )
        if account_snapshots_retention_days > 0:
            summary["deleted_account_snapshots"] = await _batched_delete(
                session_factory, "account_snapshots",
                "created_at < :cutoff",
                {"cutoff": started_at - timedelta(days=account_snapshots_retention_days)},
            )
        if fills_retention_days > 0:
            summary["deleted_fills"] = await _batched_delete(
                session_factory, "fills",
                "created_at < :cutoff",
                {"cutoff": started_at - timedelta(days=fills_retention_days)},
            )

        # 2. orders / positions 按业务终态守门 + retention 时间。
        # active 状态绝不删 — 这些是 reconcile 必须维护的真实交易状态。
        if dead_records_retention_days > 0:
            dead_cutoff = started_at - timedelta(days=dead_records_retention_days)
            summary["deleted_orders"] = await _batched_delete(
                session_factory, "orders",
                "status = ANY(:statuses) AND created_at < :cutoff",
                {
                    "statuses": ["failed", "cancelled", "matched", "expired"],
                    "cutoff": dead_cutoff,
                },
            )
            summary["deleted_positions"] = await _batched_delete(
                session_factory, "positions",
                "shares = 0 AND open_buy_shares = 0 AND open_sell_shares = 0 "
                " AND pending_buy_shares = 0 AND updated_at < :cutoff",
                {"cutoff": dead_cutoff},
            )

    except Exception as exc:  # pragma: no cover - depends on live DB
        summary["error"] = str(exc)
        logger.warning("dead records purge failed", extra={"reason": str(exc)})

    summary["completed_at"] = utc_now().isoformat()
    return summary
