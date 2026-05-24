from __future__ import annotations

import time

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """构建独立的 PostgreSQL 异步引擎 + DB query 延迟自动 instrument。"""

    engine = create_async_engine(database_url, pool_pre_ping=True, echo=echo)
    # 自动 instrument: 每次 query 记 latency 到 SystemPerfMonitor
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        context._perf_start = time.time()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            elapsed_ms = (time.time() - getattr(context, "_perf_start", time.time())) * 1000
            SystemPerfMonitor.get().record_db_query(elapsed_ms, error=False, statement=statement)
        except Exception:
            pass

    @event.listens_for(engine.sync_engine, "handle_error")
    def _err(exc_ctx):
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            SystemPerfMonitor.get().record_db_query(0.0, error=True)
        except Exception:
            pass

    return engine


def build_session_factory(
    database_url: str,
    *,
    echo: bool = False,
) -> async_sessionmaker[AsyncSession]:
    engine = build_engine(database_url, echo=echo)
    return async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(
    database_url: str,
    *,
    echo: bool = False,
) -> None:
    """Create the current development schema in PostgreSQL.

    当前开发库不维护多版本 schema 适配层，直接按最新 SQLAlchemy metadata 建表。
    """

    from polymarket_trader.infra.db.models import Base

    engine = build_engine(database_url, echo=echo)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()
