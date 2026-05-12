"""audit_events 保留期清理。

每天跑一次，删除 ``created_at < now() - audit_retention_days``。让 audit_events
表稳态在 retention 窗口内的数据量——长期运行下不再无限增长。

CLAUDE.md §7「后台任务不能决定 P0 交易主链路是否继续执行」：DELETE 失败/超时
都只记日志，不抛 supervisor，不阻塞主链路。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from polymarket_trader.serialization import utc_now

logger = logging.getLogger(__name__)


async def purge_audit_events_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retention_days: int,
    batch_size: int,
) -> dict[str, Any]:
    """删除 audit_events 中超出保留期的数据。

    ``retention_days <= 0`` 跳过 purge（保留功能禁用语义，不报错）。批量 DELETE
    控制单次锁表时间，循环跑直到删完或达到安全上限（避免极端场景死循环）。
    返回 purge 统计供 supervisor 观测和 audit。
    """

    started_at = utc_now()
    summary: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "retention_days": retention_days,
        "batch_size": batch_size,
        "deleted_rows": 0,
        "batches": 0,
        "cutoff": None,
        "completed_at": None,
        "skipped": False,
        "error": None,
    }
    if retention_days <= 0:
        summary["skipped"] = True
        summary["completed_at"] = utc_now().isoformat()
        return summary

    cutoff = started_at - timedelta(days=retention_days)
    summary["cutoff"] = cutoff.isoformat()
    # ctid-based 批量 DELETE：用子查询锁定一批 row 的 ctid，主 DELETE 走索引 +
    # primary key，避免 PG 锁全表。每批最多 batch_size 行。
    delete_sql = text(
        """
        WITH expired AS (
            SELECT ctid FROM audit_events
            WHERE created_at < :cutoff
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM audit_events WHERE ctid IN (SELECT ctid FROM expired)
        """
    )
    # 上限保护：14 天累积理论 ~1.5M rows；batch=10k → 最多 200 轮足够覆盖一天
    # 累积 + 历史积压。超过则中断让下一轮 job 继续。
    max_batches = max(1, 200)
    try:
        for batch_index in range(max_batches):
            async with session_factory() as session:
                result = await session.execute(
                    delete_sql,
                    {"cutoff": cutoff, "batch_size": batch_size},
                )
                deleted = result.rowcount or 0
                await session.commit()
            summary["batches"] += 1
            summary["deleted_rows"] += deleted
            if deleted < batch_size:
                # 删干净或剩下不够一批 — 退出循环。
                break
        else:
            logger.warning(
                "audit_retention.purge_hit_batch_cap",
                extra={
                    "deleted_rows": summary["deleted_rows"],
                    "max_batches": max_batches,
                    "cutoff": summary["cutoff"],
                },
            )
    except Exception as exc:
        summary["error"] = str(exc)
        logger.warning(
            "audit_retention.purge_failed",
            exc_info=True,
            extra={"deleted_rows": summary["deleted_rows"], "cutoff": summary["cutoff"]},
        )
    summary["completed_at"] = utc_now().isoformat()
    return summary


__all__ = ["purge_audit_events_once"]
