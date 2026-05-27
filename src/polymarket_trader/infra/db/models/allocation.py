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
    trace_id: Mapped[str] = mapped_column(String(255), index=True)
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
        # Kelly 审计字段不进列（避免 schema 漂移），统一进 raw_payload；to_domain 反读。
        kelly_payload: dict[str, Any] = {}
        for key in (
            "prob_p", "prob_confidence", "price_c",
            "edge_net", "edge_gross", "fee_per_share_usdc",
            "kelly_f_star", "effective_kelly_fraction", "effective_min_stake_usdc",
        ):
            value = getattr(allocation, key)
            if value is not None:
                kelly_payload[key] = str(value)
        if allocation.capped_by is not None:
            kelly_payload["capped_by"] = allocation.capped_by
        if allocation.is_round_up_overbet:
            kelly_payload["is_round_up_overbet"] = True

        if raw_payload is not None:
            payload = dict(_json_mapping(raw_payload))
        else:
            payload = {
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
        if kelly_payload:
            payload["kelly"] = kelly_payload
        return cls(
            allocation_key=allocation_key,
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
        kelly = self.raw_payload.get("kelly") if isinstance(self.raw_payload, dict) else None
        kelly_fields: dict[str, Any] = {}
        if isinstance(kelly, dict):
            for key in (
                "prob_p", "prob_confidence", "price_c",
                "edge_net", "edge_gross", "fee_per_share_usdc",
                "kelly_f_star", "effective_kelly_fraction", "effective_min_stake_usdc",
            ):
                raw_value = kelly.get(key)
                kelly_fields[key] = _decimal(raw_value) if raw_value is not None else None
            kelly_fields["capped_by"] = kelly.get("capped_by")
            kelly_fields["is_round_up_overbet"] = bool(kelly.get("is_round_up_overbet") or False)
        else:
            kelly_fields["is_round_up_overbet"] = False
        return Allocation(
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
            **kelly_fields,
        )


__all__ = ["AllocationModel"]
