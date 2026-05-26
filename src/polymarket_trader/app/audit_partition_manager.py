"""audit_events 月度分区维护——配套 §13.4 monthly partition。

分区策略：
- 每月初创建未来 1 个月的分区（提前预留写入位置）
- 按 retention 删除超过最长 tier（30d）的分区——用 `DROP PARTITION` 替代 batch DELETE

docs/新架构方案.md §13.4。要先跑过 `infra/db/migrations/001_audit_events_monthly_partition.sql`
把 audit_events 转成 partitioned 表；本模块仅做日常维护。

# 设计

- `ensure_future_partitions(session_factory, months_ahead=2)`：
  预创建未来 N 个月的分区，幂等（CREATE TABLE IF NOT EXISTS PARTITION OF）。
  建议在 supervisor 里按日 cadence 跑。
- `drop_expired_partitions(session_factory, retention_days=30)`：
  DROP 整个超过 retention_days 的分区，1-2 秒清理整月数据。
  与 `app/audit_retention.py` 的 batch DELETE 互补——recent 月份内部按 tier 删，
  古老月份整月 DROP。

# 检测分区表

`is_audit_partitioned(session_factory)` 检查 `audit_events` 是否是分区表
（pg_partitioned_table 视图）。supervisor 启动时检查；非分区表则跳过两个 job
（让 batch DELETE 路径继续工作）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


def _partition_name(year: int, month: int) -> str:
    return f"audit_events_y{year:04d}m{month:02d}"


def _month_range(year: int, month: int) -> tuple[date, date]:
    """返回 [start_of_month, start_of_next_month)。"""
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end


def _add_months(d: date, n: int) -> date:
    """日期加 n 个月（仅返回月初）。"""
    month0 = d.month - 1 + n
    year = d.year + month0 // 12
    month = month0 % 12 + 1
    return date(year, month, 1)


async def is_audit_partitioned(session_factory: async_sessionmaker[AsyncSession]) -> bool:
    """通过 pg_partitioned_table 判断 audit_events 是否分区表。"""

    sql = text(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_partitioned_table pt
            JOIN pg_class c ON c.oid = pt.partrelid
            WHERE c.relname = 'audit_events'
        ) AS is_partitioned
        """
    )
    try:
        async with session_factory() as session:
            row = (await session.execute(sql)).first()
        return bool(row and row[0])
    except Exception as exc:
        logger.warning(
            "audit_partition.is_partitioned_check_failed",
            extra={"error": str(exc)[:200]},
        )
        return False


async def ensure_future_partitions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    months_ahead: int = 2,
) -> dict[str, Any]:
    """预创建未来 N 个月的分区。

    幂等——已存在的分区不报错（用 CREATE TABLE IF NOT EXISTS PARTITION OF）。
    建议在 supervisor 里日级 cadence 跑（每天 0:30 UTC 检查一次）。
    """

    now = datetime.now(tz=timezone.utc).date()
    created: list[str] = []
    errors: list[str] = []

    for offset in range(months_ahead + 1):
        month_start = _add_months(date(now.year, now.month, 1), offset)
        next_month = _add_months(month_start, 1)
        partition = _partition_name(month_start.year, month_start.month)
        sql = text(
            f"""
            CREATE TABLE IF NOT EXISTS {partition}
            PARTITION OF audit_events
            FOR VALUES FROM ('{month_start.isoformat()}') TO ('{next_month.isoformat()}')
            """
        )
        try:
            async with session_factory() as session:
                await session.execute(sql)
                await session.commit()
            created.append(partition)
        except Exception as exc:
            err_text = f"{partition}: {str(exc)[:200]}"
            errors.append(err_text)
            logger.warning(
                "audit_partition.ensure_failed",
                extra={"partition": partition, "error": err_text},
            )

    return {
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
        "months_ahead": months_ahead,
        "checked_partitions": created,
        "errors": errors,
    }


async def list_partitions(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[dict[str, Any]]:
    """列出 audit_events 当前所有分区及其范围 + 行数估计。"""

    sql = text(
        """
        SELECT
            child.relname AS partition_name,
            pg_get_expr(child.relpartbound, child.oid) AS partition_range,
            pg_total_relation_size(child.oid) AS total_size_bytes,
            COALESCE(s.n_live_tup, 0) AS estimated_rows
        FROM pg_inherits
        JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
        JOIN pg_class child ON child.oid = pg_inherits.inhrelid
        LEFT JOIN pg_stat_user_tables s ON s.relid = child.oid
        WHERE parent.relname = 'audit_events'
        ORDER BY child.relname
        """
    )
    try:
        async with session_factory() as session:
            rows = (await session.execute(sql)).all()
    except Exception as exc:
        logger.warning(
            "audit_partition.list_failed",
            extra={"error": str(exc)[:200]},
        )
        return []
    return [
        {
            "partition_name": r[0],
            "range": r[1],
            "total_size_bytes": int(r[2] or 0),
            "estimated_rows": int(r[3] or 0),
        }
        for r in rows
    ]


async def drop_expired_partitions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retention_days: int = 30,
) -> dict[str, Any]:
    """DROP 超过 retention_days 的整个分区。

    保守起见：只 DROP 整月 *上限时间* 都早于 cutoff 的分区，避免误删仍在
    retention 内的事件。

    与 `audit_retention.purge_audit_events_once` 互补：
    - 古老月份（>retention_days）→ 本方法 DROP（秒级）
    - 当月 & 近 retention_days 内 → batch DELETE 按 tier 精细清理
    """

    cutoff = datetime.now(tz=timezone.utc).date() - timedelta(days=retention_days)
    partitions = await list_partitions(session_factory)
    dropped: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    # `partition_range` 形如 `FOR VALUES FROM ('2026-02-01') TO ('2026-03-01')`
    import re

    range_re = re.compile(r"FROM \('([\d-]+)[^']*'\) TO \('([\d-]+)[^']*'\)")
    for entry in partitions:
        m = range_re.search(entry.get("range") or "")
        if not m:
            continue
        try:
            upper = date.fromisoformat(m.group(2))
        except ValueError:
            continue
        if upper > cutoff:
            skipped.append(entry["partition_name"])
            continue
        # 整月 upper bound < cutoff → 整月数据已过 retention，可 DROP
        name = entry["partition_name"]
        sql = text(f"DROP TABLE IF EXISTS {name}")
        try:
            async with session_factory() as session:
                await session.execute(sql)
                await session.commit()
            dropped.append(name)
            logger.info(
                "audit_partition.dropped",
                extra={"partition": name, "estimated_rows": entry["estimated_rows"]},
            )
        except Exception as exc:
            err = f"{name}: {str(exc)[:200]}"
            errors.append(err)
            logger.warning(
                "audit_partition.drop_failed",
                extra={"partition": name, "error": err},
            )

    return {
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
        "retention_days": retention_days,
        "cutoff_date": cutoff.isoformat(),
        "dropped": dropped,
        "skipped_inside_retention": skipped,
        "errors": errors,
    }


__all__ = [
    "drop_expired_partitions",
    "ensure_future_partitions",
    "is_audit_partitioned",
    "list_partitions",
]
