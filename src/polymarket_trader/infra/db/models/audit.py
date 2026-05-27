from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.events import AuditEvent
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _json_mapping,
)
from polymarket_trader.app.audit.deduper import payload_hash as _payload_hash


class AuditEventModel(Base, TimestampMixin):
    """审计事件表。

    记录脱敏后的事件 payload，便于复盘，但不作为交易真相来源。
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(255), index=True)
    event_title: Mapped[str] = mapped_column(String(128), index=True)
    market_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    event_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    condition_id: Mapped[str | None] = mapped_column(String(128), index=True)
    token_id: Mapped[str | None] = mapped_column(String(128), index=True)
    outcome: Mapped[str | None] = mapped_column(String(64), index=True)
    side: Mapped[str | None] = mapped_column(String(16), index=True)
    order_type: Mapped[str | None] = mapped_column(String(16), index=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    size: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    notional_usdc: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    order_id: Mapped[str | None] = mapped_column(String(128), index=True)
    trade_id: Mapped[str | None] = mapped_column(String(128), index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str | None] = mapped_column(String(64), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    # 原架构方案 §13.4 dedupe 三件套：
    payload_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    occurrence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_audit_events_trace_event", "trace_id", "event_title"),
        Index("ix_audit_events_trace_order_trade", "trace_id", "order_id", "trade_id"),
        Index("ix_audit_events_dedupe_lookup", "event_title", "condition_id", "payload_hash"),
        # 注:created_at 单列索引由 TimestampMixin.index=True 自动创建为
        # ix_audit_events_created_at,不重复显式定义(SQLAlchemy 会冲突).
    )

    @classmethod
    def from_domain(
        cls,
        audit_event: AuditEvent,
        *,
        raw_payload: JsonMapping | None = None,
    ) -> "AuditEventModel":
        payload = _json_mapping(raw_payload) if raw_payload is not None else audit_event.to_payload()
        return cls(
            event_id=audit_event.event_id,
            trace_id=audit_event.trace_id,
            event_title=audit_event.event_title,
            market_slug=audit_event.market_slug,
            event_slug=audit_event.event_slug,
            condition_id=audit_event.condition_id,
            token_id=audit_event.token_id,
            outcome=audit_event.outcome,
            side=audit_event.side,
            order_type=audit_event.order_type,
            price=audit_event.price,
            size=audit_event.size,
            notional_usdc=audit_event.notional_usdc,
            order_id=audit_event.order_id,
            trade_id=audit_event.trade_id,
            tx_hash=audit_event.tx_hash,
            status=audit_event.status,
            reason=audit_event.reason,
            raw_response=audit_event.raw_response,
            payload=payload,
            payload_hash=_payload_hash(payload),
            occurrence_count=1,
            last_seen_at=audit_event.created_at,
        )

    def to_domain(self) -> AuditEvent:
        return AuditEvent(
            event_title=self.event_title,
            event_slug=self.event_slug,
            payload=dict(self.payload),
            trace_id=self.trace_id,
            created_at=self.created_at,
        )


__all__ = ["AuditEventModel"]
