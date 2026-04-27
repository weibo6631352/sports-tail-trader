from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """构建独立的 PostgreSQL 异步引擎。

    这里的连接池只服务数据库持久化链路，不能和交易 REST / WS 客户端混用。
    """

    return create_async_engine(database_url, pool_pre_ping=True, echo=echo)


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
