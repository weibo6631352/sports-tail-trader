from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.order import Order, OrderResult, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _db_key,
    _decimal,
    _ensure_aware,
    _json_bool,
    _json_mapping,
    _json_safe,
    _level_from_json,
    _level_to_json,
    _market_pause_payloads,
    _market_pauses_from_payload,
    _order_key,
    _utc_now,
)


class OrderbookSnapshotModel(Base, TimestampMixin):
    """本地 orderbook 快照。

    只保留热路径需要的盘口字段和一份 raw payload，便于恢复和审计。
    """

    __tablename__ = "orderbook_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
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


class PositionModel(Base, TimestampMixin):
    """持仓热状态和恢复参考。"""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    condition_id: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str] = mapped_column(String(128), index=True)
    market_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    shares: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    cost_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    open_buy_shares: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    open_sell_shares: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    pending_buy_shares: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    confirmed_shares: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    last_order_id: Mapped[str | None] = mapped_column(String(128), index=True)
    last_trade_id: Mapped[str | None] = mapped_column(String(128), index=True)
    confirmation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    initial_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    cash_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    percent_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    percent_realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    cur_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    redeemable: Mapped[bool | None] = mapped_column(Boolean)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index("ix_positions_trace_condition_token", "trace_id", "condition_id", "token_id"),
        Index("ix_positions_strategy_created", "strategy_id", "created_at"),
    )

    @classmethod
    def from_domain(
        cls,
        position: Position,
        *,
        trace_id: str | None = None,
        raw_payload: JsonMapping | None = None,
    ) -> "PositionModel":
        position_key = "|".join([position.condition_id, position.token_id])
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
            "strategy_id": position.strategy_id,
            "trace_id": trace_id,
            "condition_id": position.condition_id,
            "token_id": position.token_id,
            "market_slug": position.market_slug,
            "shares": str(position.shares),
            "cost_usdc": str(position.cost_usdc),
            "open_buy_shares": str(position.open_buy_shares),
            "open_sell_shares": str(position.open_sell_shares),
            "pending_buy_shares": str(position.pending_buy_shares),
            "confirmed_shares": str(position.confirmed_shares),
            "last_order_id": position.last_order_id,
            "last_trade_id": position.last_trade_id,
            "confirmation_status": position.confirmation_status,
            "updated_at": position.updated_at,
            "avg_price": str(position.avg_price) if position.avg_price is not None else None,
            "initial_value": str(position.initial_value) if position.initial_value is not None else None,
            "current_value": str(position.current_value) if position.current_value is not None else None,
            "cash_pnl": str(position.cash_pnl) if position.cash_pnl is not None else None,
            "percent_pnl": str(position.percent_pnl) if position.percent_pnl is not None else None,
            "realized_pnl": str(position.realized_pnl) if position.realized_pnl is not None else None,
            "percent_realized_pnl": str(position.percent_realized_pnl) if position.percent_realized_pnl is not None else None,
            "cur_price": str(position.cur_price) if position.cur_price is not None else None,
            "redeemable": position.redeemable,
        }
        return cls(
            position_key=position_key,
            strategy_id=position.strategy_id,
            trace_id=trace_id,
            condition_id=position.condition_id,
            token_id=position.token_id,
            market_slug=position.market_slug,
            shares=position.shares,
            cost_usdc=position.cost_usdc,
            open_buy_shares=position.open_buy_shares,
            open_sell_shares=position.open_sell_shares,
            pending_buy_shares=position.pending_buy_shares,
            confirmed_shares=position.confirmed_shares,
            last_order_id=position.last_order_id,
            last_trade_id=position.last_trade_id,
            confirmation_status=position.confirmation_status,
            avg_price=position.avg_price,
            initial_value=position.initial_value,
            current_value=position.current_value,
            cash_pnl=position.cash_pnl,
            percent_pnl=position.percent_pnl,
            realized_pnl=position.realized_pnl,
            percent_realized_pnl=position.percent_realized_pnl,
            cur_price=position.cur_price,
            redeemable=position.redeemable,
            raw_payload=payload,
        )

    def to_domain(self) -> Position:
        return Position(
            strategy_id=self.strategy_id,
            condition_id=self.condition_id,
            token_id=self.token_id,
            shares=_decimal(self.shares) or Decimal("0"),
            cost_usdc=_decimal(self.cost_usdc) or Decimal("0"),
            market_slug=self.market_slug,
            open_buy_shares=_decimal(self.open_buy_shares) or Decimal("0"),
            open_sell_shares=_decimal(self.open_sell_shares) or Decimal("0"),
            pending_buy_shares=_decimal(self.pending_buy_shares) or Decimal("0"),
            confirmed_shares=_decimal(self.confirmed_shares) or Decimal("0"),
            last_order_id=self.last_order_id,
            last_trade_id=self.last_trade_id,
            confirmation_status=self.confirmation_status,
            updated_at=self.updated_at,
            avg_price=_decimal(self.avg_price),
            initial_value=_decimal(self.initial_value),
            current_value=_decimal(self.current_value),
            cash_pnl=_decimal(self.cash_pnl),
            percent_pnl=_decimal(self.percent_pnl),
            realized_pnl=_decimal(self.realized_pnl),
            percent_realized_pnl=_decimal(self.percent_realized_pnl),
            cur_price=_decimal(self.cur_price),
            redeemable=self.redeemable,
        )


class AccountSnapshotModel(Base, TimestampMixin):
    """账户余额、买入闸门和净值快照。

    append-only 时间序列：每次写入都是一行 ``(account_key, recorded_at)`` 复合键
    的新记录。恢复路径按 ``recorded_at DESC LIMIT 1`` 读取最新一行；
    ``GET /portfolio/equity-curve`` 等审计路径按时间窗 + downsampling 聚合。
    ``net_value_usdc`` 在写入时一次性算好（``balance + Σ(position.current_value)``），
    避免事后回放还要依赖历史 mark 价格。
    """

    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
        index=True,
    )
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    balance_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    allowance_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    net_value_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    user_ws_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_new_entries: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    market_pauses: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    last_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index("ix_account_snapshots_account_recorded", "account_key", "recorded_at"),
    )

    @classmethod
    def from_domain(
        cls,
        snapshot: AccountSnapshot,
        *,
        trace_id: str | None = None,
        raw_payload: JsonMapping | None = None,
        account_key: str = "primary",
        recorded_at: datetime | None = None,
        net_value_usdc: Decimal | None = None,
    ) -> "AccountSnapshotModel":
        net_value = (
            net_value_usdc
            if net_value_usdc is not None
            else _net_value_from_snapshot(snapshot)
        )
        recorded = _ensure_aware(recorded_at) if recorded_at is not None else _utc_now()
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
            "account_key": account_key,
            "trace_id": trace_id,
            "recorded_at": _json_safe(recorded),
            "balance_usdc": str(snapshot.balance_usdc),
            "allowance_usdc": str(snapshot.allowance_usdc),
            "net_value_usdc": str(net_value),
            "user_ws_connected": snapshot.user_ws_connected,
            "allow_new_entries": snapshot.allow_new_entries,
            "market_pauses": [pause.as_payload() for pause in snapshot.market_pauses],
            "last_reconcile_at": _json_safe(snapshot.last_reconcile_at),
        }
        return cls(
            account_key=account_key,
            recorded_at=recorded,
            trace_id=trace_id,
            balance_usdc=snapshot.balance_usdc,
            allowance_usdc=snapshot.allowance_usdc,
            net_value_usdc=net_value,
            user_ws_connected=snapshot.user_ws_connected,
            allow_new_entries=snapshot.allow_new_entries,
            market_pauses=_market_pause_payloads(snapshot.market_pauses),
            last_reconcile_at=snapshot.last_reconcile_at,
            raw_payload=payload,
        )

    def to_domain(self) -> AccountSnapshot:
        return AccountSnapshot(
            balance_usdc=_decimal(self.balance_usdc) or Decimal("0"),
            allowance_usdc=_decimal(self.allowance_usdc) or Decimal("0"),
            user_ws_connected=bool(self.user_ws_connected),
            allow_new_entries=bool(self.allow_new_entries),
            market_pauses=_market_pauses_from_payload(self.market_pauses),
            last_reconcile_at=(
                None if self.last_reconcile_at is None else _ensure_aware(self.last_reconcile_at)
            ),
        )


def _net_value_from_snapshot(snapshot: AccountSnapshot) -> Decimal:
    """按 ``balance + Σ(position.current_value)`` 计算净值。

    历史净值在写入时定格——不再依赖外部历史 mark 价格，否则无法回放。
    缺失 ``current_value`` 的仓位按 0 计；reconciler 会持续刷新 mark 价格。
    """

    total = snapshot.balance_usdc or Decimal("0")
    for position in snapshot.positions:
        value = position.current_value
        if value is None:
            continue
        total += value
    return total


class AuditEventModel(Base, TimestampMixin):
    """审计事件表。

    记录脱敏后的事件 payload，便于复盘，但不作为交易真相来源。
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
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

    __table_args__ = (
        Index("ix_audit_events_trace_event", "trace_id", "event_title"),
        Index("ix_audit_events_trace_order_trade", "trace_id", "order_id", "trade_id"),
        Index("ix_audit_events_strategy_created", "strategy_id", "created_at"),
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
            strategy_id=audit_event.strategy_id,
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
        )

    def to_domain(self) -> AuditEvent:
        return AuditEvent(
            event_title=self.event_title,
            event_slug=self.event_slug,
            payload=dict(self.payload),
            trace_id=self.trace_id,
            created_at=self.created_at,
            strategy_id=self.strategy_id,
        )


class OutboxEventModel(Base, TimestampMixin):
    """本地可靠 outbox 事件。"""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
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
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
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
        Index("ix_decision_records_strategy_created", "strategy_id", "created_at"),
    )

    @classmethod
    def from_domain(cls, record: DecisionRecord) -> "DecisionRecordModel":
        return cls(
            record_id=record.record_id,
            strategy_id=record.strategy_id,
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
            strategy_id=self.strategy_id,
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
