"""Database infrastructure."""

from polymarket_trader.infra.db.models import (
    AccountSnapshotModel,
    AllocationModel,
    AuditEventModel,
    Base,
    FillModel,
    MarketModel,
    OrderModel,
    OrderbookSnapshotModel,
    OutboxEventModel,
    PositionModel,
)
from polymarket_trader.infra.db.persistence import DatabasePersistenceRepository
from polymarket_trader.infra.db.repositories import (
    AccountSnapshotRepository,
    AllocationRepository,
    AuditEventRepository,
    BaseRepository,
    FillRepository,
    MarketRepository,
    OrderRepository,
    OrderbookSnapshotRepository,
    OutboxEventRepository,
    PositionRepository,
    RepositoryPage,
)
from polymarket_trader.infra.db.session import build_engine, build_session_factory, initialize_database

__all__ = [
    "AccountSnapshotModel",
    "AccountSnapshotRepository",
    "AllocationModel",
    "AllocationRepository",
    "AuditEventModel",
    "AuditEventRepository",
    "Base",
    "BaseRepository",
    "DatabasePersistenceRepository",
    "FillModel",
    "FillRepository",
    "MarketModel",
    "MarketRepository",
    "OrderModel",
    "OrderRepository",
    "OrderbookSnapshotModel",
    "OrderbookSnapshotRepository",
    "OutboxEventModel",
    "OutboxEventRepository",
    "PositionModel",
    "PositionRepository",
    "RepositoryPage",
    "build_engine",
    "build_session_factory",
    "initialize_database",
]
