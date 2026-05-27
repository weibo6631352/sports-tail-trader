from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _decimal,
    _ensure_aware,
    _json_mapping,
    _level_from_json,
    _level_to_json,
)


class OrderbookSnapshotModel(Base, TimestampMixin):
    """本地 orderbook 快照。

    只保留热路径需要的盘口字段和一份 raw payload，便于恢复和审计。
    """

    __tablename__ = "orderbook_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(255), index=True)
    source: Mapped[str | None] = mapped_column(String(32), index=True)
    token_id: Mapped[str] = mapped_column(String(128), index=True)
    condition_id: Mapped[str | None] = mapped_column(String(128), index=True)
    market_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    best_bid_size: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    best_ask_size: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    last_trade_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    bids: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    asks: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index("ix_orderbook_snapshots_token_received_at", "token_id", "received_at"),
    )

    @classmethod
    def from_domain(
        cls,
        snapshot: OrderbookSnapshot,
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payload: JsonMapping | None = None,
        snapshot_key: str | None = None,
    ) -> "OrderbookSnapshotModel":
        snapshot_key = snapshot_key or "|".join(
            [
                snapshot.token_id,
                _ensure_aware(snapshot.received_at).isoformat(),
                "" if snapshot.best_bid is None else str(snapshot.best_bid),
                "" if snapshot.best_ask is None else str(snapshot.best_ask),
            ]
        )
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
            "token_id": snapshot.token_id,
            "market_slug": snapshot.market_slug,
            "condition_id": snapshot.condition_id,
            "best_bid": str(snapshot.best_bid) if snapshot.best_bid is not None else None,
            "best_ask": str(snapshot.best_ask) if snapshot.best_ask is not None else None,
            "best_bid_size": str(snapshot.best_bid_size) if snapshot.best_bid_size is not None else None,
            "best_ask_size": str(snapshot.best_ask_size) if snapshot.best_ask_size is not None else None,
            "last_trade_price": str(snapshot.last_trade_price) if snapshot.last_trade_price is not None else None,
            "tick_size": str(snapshot.tick_size) if snapshot.tick_size is not None else None,
            "received_at": snapshot.received_at,
            "bids": [_level_to_json(level) for level in snapshot.bids],
            "asks": [_level_to_json(level) for level in snapshot.asks],
        }
        return cls(
            snapshot_key=snapshot_key,
            trace_id=trace_id,
            source=source,
            token_id=snapshot.token_id,
            condition_id=snapshot.condition_id,
            market_slug=snapshot.market_slug,
            received_at=snapshot.received_at,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            best_bid_size=snapshot.best_bid_size,
            best_ask_size=snapshot.best_ask_size,
            last_trade_price=snapshot.last_trade_price,
            tick_size=snapshot.tick_size,
            bids=[_level_to_json(level) for level in snapshot.bids],
            asks=[_level_to_json(level) for level in snapshot.asks],
            raw_payload=payload,
        )

    def to_domain(self) -> OrderbookSnapshot:
        return OrderbookSnapshot(
            token_id=self.token_id,
            best_bid=_decimal(self.best_bid),
            best_ask=_decimal(self.best_ask),
            bids=tuple(_level_from_json(level) for level in self.bids),
            asks=tuple(_level_from_json(level) for level in self.asks),
            received_at=self.received_at,
            market_slug=self.market_slug,
            condition_id=self.condition_id,
            best_bid_size=_decimal(self.best_bid_size),
            best_ask_size=_decimal(self.best_ask_size),
            last_trade_price=_decimal(self.last_trade_price),
            tick_size=_decimal(self.tick_size),
        )


__all__ = ["OrderbookSnapshotModel"]
