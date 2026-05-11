from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _decimal,
    _json_mapping,
)


class AllocationModel(Base, TimestampMixin):
    """组合分配快照。

    记录等权预算、释放额度和本轮分配原因，供后续恢复和审计。
    """

    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    allocation_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # strategy_id NOT NULL，无 server_default。策略归属由调用侧显式提供。
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    condition_id: Mapped[str] = mapped_column(String(128), index=True)
    market_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    token_id: Mapped[str | None] = mapped_column(String(128), index=True)
    target_budget_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    buy_budget_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    current_exposure_usdc: Mapped[Decimal] = mapped_column(
        Numeric(38, 18),
        nullable=False,
        default=Decimal("0"),
    )
    released_budget_usdc: Mapped[Decimal] = mapped_column(
        Numeric(38, 18),
        nullable=False,
        default=Decimal("0"),
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    release_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index("ix_allocations_trace_condition", "trace_id", "condition_id"),
        Index("ix_allocations_strategy_created", "strategy_id", "created_at"),
    )

    @classmethod
    def from_domain(
        cls,
        allocation: Allocation,
        *,
        trace_id: str,
        raw_payload: JsonMapping | None = None,
    ) -> "AllocationModel":
        allocation_key = allocation.idempotency_key or "|".join([trace_id, allocation.condition_id])
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
            "strategy_id": allocation.strategy_id,
            "trace_id": trace_id,
            "condition_id": allocation.condition_id,
            "market_slug": allocation.market_slug,
            "token_id": allocation.token_id,
            "target_budget_usdc": str(allocation.target_budget_usdc),
            "buy_budget_usdc": str(allocation.buy_budget_usdc),
            "current_exposure_usdc": str(allocation.current_exposure_usdc),
            "released_budget_usdc": str(allocation.released_budget_usdc),
            "reason": allocation.reason,
            "release_reason": allocation.release_reason,
            "idempotency_key": allocation.idempotency_key,
        }
        return cls(
            allocation_key=allocation_key,
            strategy_id=allocation.strategy_id,
            trace_id=trace_id,
            condition_id=allocation.condition_id,
            market_slug=allocation.market_slug,
            token_id=allocation.token_id,
            target_budget_usdc=allocation.target_budget_usdc,
            buy_budget_usdc=allocation.buy_budget_usdc,
            current_exposure_usdc=allocation.current_exposure_usdc,
            released_budget_usdc=allocation.released_budget_usdc,
            reason=allocation.reason,
            release_reason=allocation.release_reason,
            idempotency_key=allocation.idempotency_key,
            raw_payload=payload,
        )

    def to_domain(self) -> Allocation:
        return Allocation(
            strategy_id=self.strategy_id,
            condition_id=self.condition_id,
            target_budget_usdc=_decimal(self.target_budget_usdc) or Decimal("0"),
            buy_budget_usdc=_decimal(self.buy_budget_usdc) or Decimal("0"),
            market_slug=self.market_slug,
            token_id=self.token_id,
            current_exposure_usdc=_decimal(self.current_exposure_usdc) or Decimal("0"),
            released_budget_usdc=_decimal(self.released_budget_usdc) or Decimal("0"),
            reason=self.reason,
            idempotency_key=self.idempotency_key,
            release_reason=self.release_reason,
        )


__all__ = ["AllocationModel"]
