"""Top-level pytest fixtures.

包含可选的 PostgreSQL 集成测试 fixture：

    pytest -q          # 默认跳过所有 @pytest.mark.pg 测试
    pytest -q --pg     # 启用，使用 testcontainers 拉起一次性 Postgres 实例

会话级 ``pg_session`` fixture 在整轮 pytest 会话内复用同一个容器和 engine；
每个用例显式 truncate 自己写入的表即可，避免容器反复启动的开销。
"""
from __future__ import annotations

from typing import AsyncIterator, Iterator

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--pg",
        action="store_true",
        default=False,
        help="Enable PostgreSQL integration tests via testcontainers.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--pg"):
        return
    skip_marker = pytest.mark.skip(reason="pg integration tests disabled (pass --pg to enable)")
    for item in items:
        if "pg" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def _pg_container() -> Iterator[object]:
    """会话级 PostgreSQL 容器；只有 ``--pg`` 模式下使用。

    导入推迟到 fixture 内部，避免无 testcontainers 环境时模块层 ImportError。
    """

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - 取决于本地 dev 环境
        pytest.skip(f"testcontainers not installed: {exc}")

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:
        pytest.skip(f"docker/postgres container unavailable: {exc}")
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def pg_database_url(_pg_container: object) -> str:
    """asyncpg 风格连接串，testcontainers 默认给的是 psycopg2 风格，需要替换 driver。"""

    url = _pg_container.get_connection_url()  # type: ignore[attr-defined]
    if url.startswith("postgresql+psycopg2://"):
        url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


@pytest.fixture(scope="session")
async def pg_engine(pg_database_url: str):
    """会话级 async engine + 一次性 metadata.create_all。"""

    from polymarket_trader.infra.db.models import Base
    from polymarket_trader.infra.db.session import build_engine

    engine = build_engine(pg_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def pg_session(pg_engine) -> AsyncIterator[object]:
    """单测用 AsyncSession；用例可在 setup 阶段 truncate 自己关心的表。"""

    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
def pg_session_factory(pg_engine):
    """对外暴露 sessionmaker，便于 service / DAO 复用同一个 engine。"""

    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(pg_engine, expire_on_commit=False)


