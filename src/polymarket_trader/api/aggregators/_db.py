"""Aggregator DB session helper —— `with_repositories` + `RepositoryGroup`。

docs/新架构方案.md §12.2 审计查询类（走 DB）。每个 aggregator 不持久 session，
统一通过 `with_repositories(session_factory, callback)` 打开 + 释放，避免长
连接 + 跨方法状态。

# 用法

```python
from polymarket_trader.api.aggregators._db import with_repositories

class TimelineAggregator:
    def __init__(self, *, session_factory): ...

    async def list_audit_events(self, ...) -> dict:
        return await with_repositories(
            self._session_factory,
            lambda repos: repos.audit.list_audit_events_snapshot(...),
        )
```

aggregator 持 `session_factory: async_sessionmaker | None`，None 时所有方法返回空 page（前端友好降级）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_trader.infra.db import (
    AllocationRepository,
    AuditEventRepository,
    DecisionRecordRepository,
    FillRepository,
    MarketRepository,
    OrderRepository,
    OrderbookSnapshotRepository,
    OutboxEventRepository,
    PositionRepository,
)


@dataclass(frozen=True, slots=True)
class RepositoryGroup:
    """所有 admin 查询用 repository 的一站式访问。

    每次 `with_repositories` 调用都创建新 session + 包装新 group——repo 对象
    持 session 引用，不能跨 session 复用。
    """

    audit: AuditEventRepository
    market: MarketRepository
    order: OrderRepository
    fill: FillRepository
    position: PositionRepository
    allocation: AllocationRepository
    decision: DecisionRecordRepository
    outbox: OutboxEventRepository
    orderbook: OrderbookSnapshotRepository


async def with_repositories(
    session_factory: async_sessionmaker[AsyncSession] | None,
    callback: Callable[[RepositoryGroup], Any],
) -> Any:
    """开一次 async session + 构造 RepositoryGroup + 调 callback。

    session_factory=None → RuntimeError（caller 应先 _has_db_session_factory 判定）。
    """

    if session_factory is None:
        raise RuntimeError("db_session_factory unavailable")
    async with session_factory() as session:
        repositories = RepositoryGroup(
            audit=AuditEventRepository(session),
            market=MarketRepository(session),
            order=OrderRepository(session),
            fill=FillRepository(session),
            position=PositionRepository(session),
            allocation=AllocationRepository(session),
            decision=DecisionRecordRepository(session),
            outbox=OutboxEventRepository(session),
            orderbook=OrderbookSnapshotRepository(session),
        )
        return await callback(repositories)
