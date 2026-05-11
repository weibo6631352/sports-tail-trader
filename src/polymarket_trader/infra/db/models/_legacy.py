from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.order import Order, OrderResult, OrderSide, OrderStatus, OrderType
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _db_key,
    _decimal,
    _json_bool,
    _json_mapping,
    _order_key,
)


class OrderModel(Base, TimestampMixin):
    """订单状态快照。

    订单表保存交易生命周期和执行结果，方便回放与恢复。
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    condition_id: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str] = mapped_column(String(128), index=True)
    market_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    amount_usdc: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    size_shares: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    filled_shares: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    remaining_shares: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    notional_usdc: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    order_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    trade_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    post_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index("ix_orders_trace_order_trade", "trace_id", "order_id", "trade_id"),
        Index("ix_orders_condition_token_status", "condition_id", "token_id", "status"),
        Index("ix_orders_strategy_created", "strategy_id", "created_at"),
    )

    @classmethod
    def from_domain(
        cls,
        order: Order | OrderResult,
        *,
        raw_payload: JsonMapping | None = None,
    ) -> "OrderModel":
        if isinstance(order, Order):
            side = order.side
            order_type = order.order_type
            order_key = _order_key(order)
            payload = _json_mapping(raw_payload) if raw_payload is not None else {
                "strategy_id": order.strategy_id,
                "trace_id": order.trace_id,
                "condition_id": order.condition_id,
                "token_id": order.token_id,
                "market_slug": order.market_slug,
                "side": order.side.value,
                "order_type": order.order_type.value,
                "price": str(order.price),
                "amount_usdc": str(order.amount_usdc) if order.amount_usdc is not None else None,
                "size_shares": str(order.size_shares) if order.size_shares is not None else None,
                "filled_shares": str(order.filled_shares),
                "remaining_shares": str(order.remaining_shares) if order.remaining_shares is not None else None,
                "notional_usdc": str(order.notional_usdc) if order.notional_usdc is not None else None,
                "order_id": order.order_id,
                "trade_id": order.trade_id,
                "status": order.status.value,
                "idempotency_key": order.idempotency_key,
                "reason": order.reason,
                "post_only": order.post_only,
            }
            return cls(
                order_key=order_key,
                strategy_id=order.strategy_id,
                trace_id=order.trace_id,
                condition_id=order.condition_id,
                token_id=order.token_id,
                market_slug=order.market_slug,
                side=side.value,
                order_type=order_type.value,
                price=order.price,
                amount_usdc=order.amount_usdc,
                size_shares=order.size_shares,
                filled_shares=order.filled_shares,
                remaining_shares=order.remaining_shares,
                notional_usdc=order.notional_usdc,
                order_id=order.order_id,
                trade_id=order.trade_id,
                status=order.status.value,
                idempotency_key=_db_key(order.idempotency_key),
                reason=order.reason,
                post_only=order.post_only,
                raw_payload=payload,
            )

        if order.side is None or order.order_type is None:
            raise ValueError("OrderResult must include side/order_type to persist in orders table")

        order_key = _order_key(order)
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
            "strategy_id": order.strategy_id,
            "trace_id": order.trace_id,
            "condition_id": order.condition_id,
            "token_id": order.token_id,
            "market_slug": order.market_slug,
            "status": order.status.value,
            "intent": None if order.intent is None else type(order.intent).__name__,
            "order_id": order.order_id,
            "trade_id": order.trade_id,
            "side": None if order.side is None else order.side.value,
            "order_type": None if order.order_type is None else order.order_type.value,
            "price": str(order.price) if order.price is not None else None,
            "requested_amount_usdc": (
                str(order.requested_amount_usdc) if order.requested_amount_usdc is not None else None
            ),
            "requested_size_shares": (
                str(order.requested_size_shares) if order.requested_size_shares is not None else None
            ),
            "matched_shares": str(order.matched_shares),
            "remaining_shares": str(order.remaining_shares),
            "spent_usdc": str(order.spent_usdc),
            "notional_usdc": str(order.notional_usdc),
            "reason": order.reason,
            "retryable": order.retryable,
            "raw_response_summary": order.raw_response_summary,
        }
        return cls(
            order_key=order_key,
            strategy_id=order.strategy_id,
            trace_id=order.trace_id,
            condition_id=order.condition_id,
            token_id=order.token_id,
            market_slug=order.market_slug,
            side="" if order.side is None else order.side.value,
            order_type="" if order.order_type is None else order.order_type.value,
            price=order.price or Decimal("0"),
            amount_usdc=order.requested_amount_usdc,
            size_shares=order.requested_size_shares,
            filled_shares=order.matched_shares,
            remaining_shares=order.remaining_shares,
            notional_usdc=order.notional_usdc,
            order_id=order.order_id,
            trade_id=order.trade_id,
            status=order.status.value,
            idempotency_key=(
                _db_key(order.intent.idempotency_key)
                if order.intent and getattr(order.intent, "idempotency_key", None)
                else None
            ),
            reason=order.reason,
            post_only=(
                bool(getattr(order.intent, "post_only", False))
                if order.intent is not None
                else _json_bool(payload.get("post_only"))
            ),
            raw_payload=payload,
        )

    def to_domain(self) -> Order:
        return Order(
            strategy_id=self.strategy_id,
            trace_id=self.trace_id,
            condition_id=self.condition_id,
            token_id=self.token_id,
            market_slug=self.market_slug,
            side=OrderSide(self.side),
            order_type=OrderType(self.order_type),
            price=_decimal(self.price) or Decimal("0"),
            amount_usdc=_decimal(self.amount_usdc),
            size_shares=_decimal(self.size_shares),
            filled_shares=_decimal(self.filled_shares) or Decimal("0"),
            remaining_shares=_decimal(self.remaining_shares),
            notional_usdc=_decimal(self.notional_usdc),
            order_id=self.order_id,
            trade_id=self.trade_id,
            status=OrderStatus(self.status),
            idempotency_key=self.idempotency_key,
            reason=self.reason,
            post_only=bool(self.post_only),
        )
