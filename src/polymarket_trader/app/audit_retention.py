"""audit_events 分层 retention 清理（docs/新架构方案.md §13.5）。

按 event_title 分层 retention：
- 交易类 / 决策类 30 天
- 拒绝类 7 天
- heartbeat / synthetic 1 天
- fills 永久（金额审计不允许丢）
- 其他默认 14 天

CLAUDE.md §7「后台任务不能决定 P0 交易主链路是否继续执行」：DELETE 失败/超时
都只记日志，不抛 supervisor，不阻塞主链路。

# 兼容旧 retention_days 参数

旧调用方式 `purge_audit_events_once(session_factory, retention_days=N, batch_size=K)`
被改造为统一前缀 ANY 用同一 cutoff——但生产路径已经切到分层模式。保留旧
签名仅为 startup smoke / 单元测试便利。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_trader.infra.db.retention_policy import (
    DEFAULT_AUDIT_RETENTION_POLICY,
    PERMANENT_RETENTION_DAYS,
    AuditRetentionPolicy,
)
from polymarket_trader.serialization import utc_now

logger = logging.getLogger(__name__)

# 单层 DELETE 上限保护：14 天累积理论 ~1.5M rows；batch=10k → 最多 200 轮足够覆盖
# 一天累积 + 历史积压。超过则中断让下一轮 job 继续。
_MAX_BATCHES_PER_TIER: int = 200


async def purge_audit_events_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retention_days: int | None = None,
    batch_size: int,
    policy: AuditRetentionPolicy | None = None,
) -> dict[str, Any]:
    """按分层 retention 清理 audit_events。

    优先级：
    - `policy` 显式传入 → 走分层 purge（生产路径）
    - 否则 `retention_days` → 单层 purge（向后兼容旧调用方式）
    - 默认走 `DEFAULT_AUDIT_RETENTION_POLICY`

    返回每层 purge 的 deleted_rows / batches，便于 supervisor 观测。
    """

    started_at = utc_now()
    summary: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "batch_size": batch_size,
        "deleted_rows": 0,
        "batches": 0,
        "tiers": [],
        "completed_at": None,
        "error": None,
    }

    if policy is None and retention_days is not None:
        # 旧签名：所有 event_title 同 retention（用于 smoke / 测试）
        return await _purge_single_tier(
            session_factory,
            cutoff=started_at - timedelta(days=retention_days),
            batch_size=batch_size,
            event_title_prefixes=(),  # 空 → 所有
            tier_label=f"legacy_{retention_days}d",
            summary=summary,
        )

    active_policy = policy or DEFAULT_AUDIT_RETENTION_POLICY
    buckets = active_policy.buckets_by_days()
    # 分层 purge：跳过 PERMANENT（永久保留）+ ≤0 天 tier；其他按 days 算 cutoff
    for days, prefixes in buckets.items():
        if days == PERMANENT_RETENTION_DAYS:
            continue
        if days <= 0:
            continue
        cutoff = started_at - timedelta(days=days)
        tier_summary = await _purge_single_tier(
            session_factory,
            cutoff=cutoff,
            batch_size=batch_size,
            event_title_prefixes=prefixes,
            tier_label=f"{days}d",
            summary={"deleted_rows": 0, "batches": 0},
        )
        summary["tiers"].append(
            {
                "days": days,
                "prefixes": list(prefixes),
                "cutoff": cutoff.isoformat(),
                **{k: tier_summary[k] for k in ("deleted_rows", "batches")},
                "error": tier_summary.get("error"),
            }
        )
        summary["deleted_rows"] += tier_summary["deleted_rows"]
        summary["batches"] += tier_summary["batches"]
        if tier_summary.get("error"):
            summary["error"] = tier_summary["error"]

    # fallback 兜底：未列入 tier 的 event_title 也要按 fallback_days 删
    fallback_days = active_policy.fallback_days
    if fallback_days > 0:
        fallback_cutoff = started_at - timedelta(days=fallback_days)
        # 收集所有 tier 已覆盖的 prefix（NOT IN 用）
        covered_prefixes: list[str] = []
        for days, prefixes in buckets.items():
            covered_prefixes.extend(prefixes)
        fallback_summary = await _purge_fallback(
            session_factory,
            cutoff=fallback_cutoff,
            batch_size=batch_size,
            covered_prefixes=tuple(covered_prefixes),
        )
        summary["tiers"].append(
            {
                "days": fallback_days,
                "prefixes": ["<fallback>"],
                "cutoff": fallback_cutoff.isoformat(),
                **{k: fallback_summary[k] for k in ("deleted_rows", "batches")},
                "error": fallback_summary.get("error"),
            }
        )
        summary["deleted_rows"] += fallback_summary["deleted_rows"]
        summary["batches"] += fallback_summary["batches"]
        if fallback_summary.get("error"):
            summary["error"] = fallback_summary["error"]

    summary["completed_at"] = utc_now().isoformat()
    return summary


async def _purge_single_tier(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cutoff: datetime,
    batch_size: int,
    event_title_prefixes: tuple[str, ...],
    tier_label: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """对单 tier（event_title prefix 集合）按 cutoff 批量 DELETE。

    `event_title_prefixes=()` 表示不限制 prefix（旧签名行为）。
    """

    if event_title_prefixes:
        # 用 `LIKE` 前缀匹配；多 prefix 用 OR 拼装
        like_clauses = " OR ".join(
            f"event_title LIKE :prefix_{i} || '%'" for i in range(len(event_title_prefixes))
        )
        delete_sql = text(
            f"""
            WITH expired AS (
                SELECT ctid FROM audit_events
                WHERE created_at < :cutoff AND ({like_clauses})
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM audit_events WHERE ctid IN (SELECT ctid FROM expired)
            """
        )
    else:
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

    params: dict[str, Any] = {"cutoff": cutoff, "batch_size": batch_size}
    for i, prefix in enumerate(event_title_prefixes):
        params[f"prefix_{i}"] = prefix

    try:
        for _ in range(_MAX_BATCHES_PER_TIER):
            async with session_factory() as session:
                result = await session.execute(delete_sql, params)
                deleted = result.rowcount or 0
                await session.commit()
            summary["batches"] += 1
            summary["deleted_rows"] += deleted
            if deleted < batch_size:
                break
        else:
            logger.warning(
                "audit_retention.tier_hit_batch_cap",
                extra={
                    "tier": tier_label,
                    "deleted_rows": summary["deleted_rows"],
                    "cutoff": cutoff.isoformat(),
                },
            )
    except Exception as exc:
        summary["error"] = str(exc)
        logger.warning(
            "audit_retention.tier_failed",
            exc_info=True,
            extra={
                "tier": tier_label,
                "deleted_rows": summary["deleted_rows"],
                "cutoff": cutoff.isoformat(),
            },
        )
    return summary


async def _purge_fallback(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cutoff: datetime,
    batch_size: int,
    covered_prefixes: tuple[str, ...],
) -> dict[str, Any]:
    """删除未被任何显式 tier 覆盖、且超过 fallback_days 的事件。

    用 NOT (event_title LIKE prefix1 OR prefix2 ...) 排除已覆盖部分。
    """

    summary: dict[str, Any] = {"deleted_rows": 0, "batches": 0, "error": None}
    if not covered_prefixes:
        # 没有任何 tier prefix → fallback 等价于 single tier 全删
        return await _purge_single_tier(
            session_factory,
            cutoff=cutoff,
            batch_size=batch_size,
            event_title_prefixes=(),
            tier_label="fallback_all",
            summary=summary,
        )
    not_like_clauses = " AND ".join(
        f"event_title NOT LIKE :prefix_{i} || '%'"
        for i in range(len(covered_prefixes))
    )
    delete_sql = text(
        f"""
        WITH expired AS (
            SELECT ctid FROM audit_events
            WHERE created_at < :cutoff AND ({not_like_clauses})
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM audit_events WHERE ctid IN (SELECT ctid FROM expired)
        """
    )
    params: dict[str, Any] = {"cutoff": cutoff, "batch_size": batch_size}
    for i, prefix in enumerate(covered_prefixes):
        params[f"prefix_{i}"] = prefix

    try:
        for _ in range(_MAX_BATCHES_PER_TIER):
            async with session_factory() as session:
                result = await session.execute(delete_sql, params)
                deleted = result.rowcount or 0
                await session.commit()
            summary["batches"] += 1
            summary["deleted_rows"] += deleted
            if deleted < batch_size:
                break
    except Exception as exc:
        summary["error"] = str(exc)
        logger.warning(
            "audit_retention.fallback_failed",
            exc_info=True,
            extra={"deleted_rows": summary["deleted_rows"], "cutoff": cutoff.isoformat()},
        )
    return summary


__all__ = ["purge_audit_events_once"]
