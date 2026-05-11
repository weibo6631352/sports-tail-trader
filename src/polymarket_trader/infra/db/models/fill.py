from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.events import Fill
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _decimal,
    _json_mapping,
)


class FillModel(Base, TimestampMixin):
    """成交确认快照。"""

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    condition_id: Mapped[str | None] = mapped_column(String(128), index=True)
    token_id: Mapped[str | None] = mapped_column(String(128), index=True)
    market_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    order_id: Mapped[str | None] = mapped_column(String(128), index=True)
    trade_id: Mapped[str | None] = mapped_column(String(128), index=True)
    side: Mapped[str | None] = mapped_column(String(8), index=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    size: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    notional_usdc: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index("ix_fills_trace_trade_order", "trace_id", "trade_id", "order_id"),
        Index("ix_fills_strategy_created", "strategy_id", "created_at"),
    )

    @classmethod
    def from_domain(
        cls,
        fill: Fill,
        *,
        raw_payload: JsonMapping | None = None,
    ) -> "FillModel":
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
            "strategy_id": fill.strategy_id,
            "trace_id": fill.trace_id,
            "event_type": str(fill.event_type),
            "event_id": fill.event_id,
            "market_slug": fill.market_slug,
            "condition_id": fill.condition_id,
            "token_id": fill.token_id,
            "order_id": fill.order_id,
            "trade_id": fill.trade_id,
            "side": fill.side,
            "price": str(fill.price) if fill.price is not None else None,
            "size": str(fill.size) if fill.size is not None else None,
            "notional_usdc": str(fill.notional_usdc) if fill.notional_usdc is not None else None,
            "status": fill.status,
            "confirmed_at": fill.confirmed_at,
        }
        return cls(
            event_id=fill.event_id,
            strategy_id=fill.strategy_id,
            trace_id=fill.trace_id,
            event_type=str(fill.event_type),
            condition_id=fill.condition_id,
            token_id=fill.token_id,
            market_slug=fill.market_slug,
            order_id=fill.order_id,
            trade_id=fill.trade_id,
            side=fill.side,
            price=fill.price,
            size=fill.size,
            notional_usdc=fill.notional_usdc,
            status=fill.status,
            confirmed_at=fill.confirmed_at,
            raw_payload=payload,
        )

    def to_domain(self) -> Fill:
        return Fill(
            strategy_id=self.strategy_id,
            trace_id=self.trace_id,
            event_type=self.event_type,
            event_id=self.event_id,
            market_slug=self.market_slug,
            condition_id=self.condition_id,
            token_id=self.token_id,
            order_id=self.order_id,
            trade_id=self.trade_id,
            side=self.side,
            price=_decimal(self.price),
            size=_decimal(self.size),
            notional_usdc=_decimal(self.notional_usdc),
            status=self.status,
            confirmed_at=self.confirmed_at,
        )


__all__ = ["FillModel"]
