"""Repository aggregate.

One module per aggregate root; ``_legacy.py`` keeps remaining repositories
during the split. Re-exports keep ``from polymarket_trader.infra.db
import ...`` and other call sites stable.
"""

from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
)
from polymarket_trader.infra.db.repositories._legacy import (
    AccountHistoryPoint,
    AccountSnapshotRepository,
    DecisionRecordRepository,
    FillRepository,
    OrderRepository,
    OutboxEventRepository,
    PositionRepository,
)
from polymarket_trader.infra.db.repositories.allocation import AllocationRepository
from polymarket_trader.infra.db.repositories.audit import AuditEventRepository
from polymarket_trader.infra.db.repositories.market import MarketRepository
from polymarket_trader.infra.db.repositories.orderbook import OrderbookSnapshotRepository

__all__ = [
    "AccountHistoryPoint",
    "AccountSnapshotRepository",
    "AllocationRepository",
    "AuditEventRepository",
    "BaseRepository",
    "DecisionRecordRepository",
    "FillRepository",
    "MarketRepository",
    "OrderRepository",
    "OrderbookSnapshotRepository",
    "OutboxEventRepository",
    "PositionRepository",
    "RepositoryPage",
]
