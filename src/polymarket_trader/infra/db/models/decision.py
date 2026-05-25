from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.infra.db.base import (
    Base,
    TimestampMixin,
    _json_mapping,
)


class DecisionRecordModel(Base, TimestampMixin):
    """策略 hook 决策录制表。

    append-only 时间序列：每次 ``decide_entry`` / ``decide_exit`` 等 hook
    返回结果都会写一行，作为离线复盘与策略回归对比的权威来源。
    ``created_at`` 作为时间维度索引，``accepted`` / ``reason`` 用于
    dump 端点的拒绝原因聚合查询。
    """

    __tablename__ = "decision_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    hook_name: Mapped[str | None] = mapped_column(String(64), index=True)
    condition_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token_id: Mapped[str | None] = mapped_column(String(128), index=True)
    market_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    decision_input: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    decision_output: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, index=True)

    __table_args__ = (
        Index("ix_decision_records_trace_created", "trace_id", "created_at"),
        Index("ix_decision_records_condition_created", "condition_id", "created_at"),
        Index("ix_decision_records_accepted_created", "accepted", "created_at"),
    )

    @classmethod
    def from_domain(cls, record: DecisionRecord) -> "DecisionRecordModel":
        return cls(
            record_id=record.record_id,
            trace_id=record.trace_id,
            hook_name=record.hook_name or None,
            condition_id=record.condition_id,
            token_id=record.token_id,
            market_slug=record.market_slug,
            decision_input=_json_mapping(record.decision_input),
            decision_output=_json_mapping(record.decision_output),
            accepted=bool(record.accepted),
            reason=record.reason,
            created_at=record.created_at,
        )

    def to_domain(self) -> DecisionRecord:
        return DecisionRecord(
            record_id=self.record_id,
            trace_id=self.trace_id,
            hook_name=self.hook_name or "",
            condition_id=self.condition_id,
            token_id=self.token_id,
            market_slug=self.market_slug,
            decision_input=dict(self.decision_input or {}),
            decision_output=dict(self.decision_output or {}),
            accepted=bool(self.accepted),
            reason=self.reason,
            created_at=self.created_at,
        )


__all__ = ["DecisionRecordModel"]
