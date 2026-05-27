from __future__ import annotations

from typing import Any

from sqlalchemy import Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.events import OutboxEvent
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _db_key,
    _json_mapping,
)


class OutboxEventModel(Base, TimestampMixin):
    """本地可靠 outbox 事件。"""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(255), index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    market_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    event_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    condition_id: Mapped[str | None] = mapped_column(String(128), index=True)
    token_id: Mapped[str | None] = mapped_column(String(128), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    raw_response_summary: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index("ix_outbox_events_trace_priority", "trace_id", "priority"),
        Index("ix_outbox_events_event_condition_token", "event_type", "condition_id", "token_id"),
    )

    @classmethod
    def from_domain(
        cls,
        event: OutboxEvent,
        *,
        raw_payload: JsonMapping | None = None,
    ) -> "OutboxEventModel":
        payload = _json_mapping(raw_payload) if raw_payload is not None else dict(event.payload)
        return cls(
            event_id=event.event_id,
            trace_id=event.trace_id,
            event_type=event.event_type,
            idempotency_key=_db_key(event.idempotency_key) or event.event_id,
            market_slug=event.market_slug,
            event_slug=event.event_slug,
            condition_id=event.condition_id,
            token_id=event.token_id,
            reason=event.reason,
            priority=event.priority,
            retry_count=event.retry_count,
            last_error=event.last_error,
            raw_response_summary=event.raw_response_summary,
            payload=payload,
        )

    def to_domain(self) -> OutboxEvent:
        return OutboxEvent(
            trace_id=self.trace_id,
            event_type=self.event_type,
            idempotency_key=self.idempotency_key,
            event_id=self.event_id,
            market_slug=self.market_slug,
            event_slug=self.event_slug,
            condition_id=self.condition_id,
            token_id=self.token_id,
            reason=self.reason,
            created_at=self.created_at,
            priority=self.priority,
            retry_count=self.retry_count,
            last_error=self.last_error,
            raw_response_summary=self.raw_response_summary,
            payload=dict(self.payload),
        )


__all__ = ["OutboxEventModel"]
