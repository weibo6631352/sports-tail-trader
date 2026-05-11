"""SQLAlchemy ORM model aggregate.

Each per-table module owns one model class; this package re-exports them
so callers (and ``Base.metadata.create_all``) see the full registry. The
``_legacy`` shim keeps current model definitions until each submodule is
extracted.
"""

from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    JsonValue,
    TimestampMixin,
    _decimal,
    _ensure_aware,
)
from polymarket_trader.infra.db.models._legacy import (
    AccountSnapshotModel,
    AuditEventModel,
    DecisionRecordModel,
    FillModel,
    OrderModel,
    OutboxEventModel,
    PositionModel,
)
from polymarket_trader.infra.db.models.allocation import AllocationModel
from polymarket_trader.infra.db.models.market import MarketModel
from polymarket_trader.infra.db.models.orderbook import OrderbookSnapshotModel

__all__ = [
    "AccountSnapshotModel",
    "AllocationModel",
    "AuditEventModel",
    "Base",
    "DecisionRecordModel",
    "FillModel",
    "JsonMapping",
    "JsonValue",
    "MarketModel",
    "OrderModel",
    "OrderbookSnapshotModel",
    "OutboxEventModel",
    "PositionModel",
    "TimestampMixin",
    "_decimal",
    "_ensure_aware",
]
