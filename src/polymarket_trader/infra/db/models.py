from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import Order, OrderResult, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.account import AccountSnapshot, MarketPause

JsonValue = Any
JsonMapping = Mapping[str, Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decimal(value: Any | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    return Decimal(text_value)


def _text(value: Any | None) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _datetime_value(value: Any | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return _ensure_aware(datetime.fromisoformat(text_value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _fee_rate_units_from_payload(payload: JsonMapping | None) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    fee_schedule = payload.get("feeSchedule")
    if not isinstance(fee_schedule, Mapping):
        fee_schedule = payload.get("fee_schedule")
    if not isinstance(fee_schedule, Mapping):
        return None
    rate = fee_schedule.get("rate")
    if rate is None:
        rate = fee_schedule.get("base_fee")
    if rate is None:
        rate = fee_schedule.get("baseFee")
    if rate is None or isinstance(rate, bool):
        return None
    numeric = _decimal(rate)
    if numeric is None or numeric < Decimal("0"):
        return None
    if numeric < Decimal("1"):
        return int((numeric * Decimal("1000")).to_integral_value())
    if numeric == numeric.to_integral_value():
        return int(numeric)
    return int((numeric * Decimal("1000")).to_integral_value())


def _json_safe(value: Any) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:
        return str(value)


def _json_mapping(value: JsonMapping | None) -> dict[str, Any]:
    if not value:
        return {}
    return {str(key): _json_safe(item) for key, item in value.items()}


def _tuple_from_sequence(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in value if str(item).strip())
    text_value = str(value).strip()
    return (text_value,) if text_value else ()


def _market_pause_payloads(pauses: tuple[MarketPause, ...]) -> list[dict[str, JsonValue]]:
    return [_json_safe(pause.as_payload()) for pause in pauses]


def _market_pauses_from_payload(value: Any) -> tuple[MarketPause, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    pauses: list[MarketPause] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        condition_id = _text(item.get("condition_id"))
        reason = _text(item.get("reason"))
        source = _text(item.get("source"))
        recoverable = item.get("recoverable")
        if condition_id is None or reason is None or source is None or not isinstance(recoverable, bool):
            continue
        try:
            pauses.append(
                MarketPause.build(
                    condition_id=condition_id,
                    reason=reason,
                    source=source,
                    recoverable=recoverable,
                )
            )
        except ValueError:
            continue
    return tuple(pauses)


def _level_to_json(level: PriceLevel) -> dict[str, str]:
    return {"price": str(level.price), "size": str(level.size)}


def _level_from_json(value: Any) -> PriceLevel:
    if isinstance(value, Mapping):
        return PriceLevel(price=Decimal(str(value["price"])), size=Decimal(str(value["size"])))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return PriceLevel(price=Decimal(str(value[0])), size=Decimal(str(value[1])))
    raise TypeError(f"unsupported price level payload: {value!r}")


def _order_key(order: Order | OrderResult) -> str:
    if isinstance(order, Order):
        if order.idempotency_key:
            return order.idempotency_key
        return "|".join(
            [
                order.trace_id or "",
                order.condition_id,
                order.token_id,
                order.side.value,
                order.order_type.value,
            ]
        )
    if order.intent is not None and getattr(order.intent, "idempotency_key", None):
        return str(order.intent.idempotency_key)
    if order.order_id:
        return order.order_id
    return "|".join(
        [
            order.trace_id,
            order.condition_id,
            order.token_id,
            "" if order.side is None else order.side.value,
            "" if order.order_type is None else order.order_type.value,
        ]
    )


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MarketModel(Base, TimestampMixin):
    """本地市场快照。

    数据库只作为审计和恢复参考，不是交易状态唯一真相来源。
    """

    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source: Mapped[str | None] = mapped_column(String(32), index=True)
    condition_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    market_slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    token_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    outcomes: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    market_name: Mapped[str | None] = mapped_column(String(512), index=True)
    market_question: Mapped[str | None] = mapped_column(Text)
    event_id: Mapped[str | None] = mapped_column(String(128), index=True)
    event_title: Mapped[str | None] = mapped_column(String(512), index=True)
    event_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    tick_size: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0.01"))
    min_order_size: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("1"))
    neg_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fees_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    maker_base_fee_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    taker_base_fee_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fee_rate_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fee_rate_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    category: Mapped[str | None] = mapped_column(String(128), index=True)
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    matched_keywords: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    trading_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index("ix_markets_trading_status_condition_id", "trading_status", "condition_id"),
    )

    @classmethod
    def from_domain(
        cls,
        market: Market,
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payload: JsonMapping | None = None,
    ) -> "MarketModel":
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
            "condition_id": market.condition_id,
            "market_slug": market.market_slug,
            "token_ids": list(market.token_ids),
            "outcomes": [
                {"token_id": outcome.token_id, "outcome": outcome.outcome}
                for outcome in market.outcomes
            ],
            "market_name": market.market_name,
            "market_question": market.market_question,
            "event_id": market.event_id,
            "event_title": market.event_title,
            "event_slug": market.event_slug,
            "icon_url": market.icon_url,
            "end_date": _json_safe(market.end_date),
            "tick_size": str(market.tick_size),
            "min_order_size": str(market.min_order_size),
            "neg_risk": market.neg_risk,
            "fees_enabled": market.fees_enabled,
            "maker_base_fee_bps": market.maker_base_fee_bps,
            "taker_base_fee_bps": market.taker_base_fee_bps,
            "fee_rate_bps": market.fee_rate_bps,
            "fee_rate_updated_at": _json_safe(market.fee_rate_updated_at),
            "category": market.category,
            "tags": list(market.tags),
            "matched_keywords": list(market.matched_keywords),
            "trading_status": market.trading_status.value,
            "reject_reason": market.reject_reason,
        }
        return cls(
            trace_id=trace_id,
            source=source,
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            token_ids=list(market.token_ids),
            outcomes=[
                {"token_id": outcome.token_id, "outcome": outcome.outcome}
                for outcome in market.outcomes
            ],
            market_name=market.market_name,
            market_question=market.market_question,
            event_id=market.event_id,
            event_title=market.event_title,
            event_slug=market.event_slug,
            tick_size=market.tick_size,
            min_order_size=market.min_order_size,
            neg_risk=market.neg_risk,
            fees_enabled=market.fees_enabled,
            maker_base_fee_bps=market.maker_base_fee_bps,
            taker_base_fee_bps=market.taker_base_fee_bps,
            fee_rate_bps=market.fee_rate_bps,
            fee_rate_updated_at=market.fee_rate_updated_at,
            category=market.category,
            tags=list(market.tags),
            matched_keywords=list(market.matched_keywords),
            trading_status=market.trading_status.value,
            reject_reason=market.reject_reason,
            raw_payload=payload,
        )

    def to_domain(self) -> Market:
        raw_payload = self.raw_payload if isinstance(self.raw_payload, Mapping) else {}
        schedule_fee_rate_bps = _fee_rate_units_from_payload(raw_payload)
        return Market(
            condition_id=self.condition_id,
            market_slug=self.market_slug,
            outcomes=tuple(
                MarketOutcome(
                    token_id=_text(item.get("token_id")) or "",
                    outcome=_text(item.get("outcome")) or "",
                )
                for item in self.outcomes
                if isinstance(item, Mapping)
                and _text(item.get("token_id"))
                and _text(item.get("outcome"))
            ),
            market_name=self.market_name,
            market_question=self.market_question,
            event_id=self.event_id,
            event_title=self.event_title,
            event_slug=self.event_slug,
            icon_url=_text(raw_payload.get("icon_url")) or _text(raw_payload.get("icon")),
            end_date=_datetime_value(raw_payload.get("end_date")) or _datetime_value(raw_payload.get("endDate")),
            tick_size=_decimal(self.tick_size) or Decimal("0.01"),
            min_order_size=_decimal(self.min_order_size) or Decimal("1"),
            neg_risk=bool(self.neg_risk),
            fees_enabled=self.fees_enabled,
            maker_base_fee_bps=self.maker_base_fee_bps,
            taker_base_fee_bps=(
                schedule_fee_rate_bps
                if schedule_fee_rate_bps is not None
                else self.taker_base_fee_bps
            ),
            fee_rate_bps=(
                schedule_fee_rate_bps if schedule_fee_rate_bps is not None else self.fee_rate_bps
            ),
            fee_rate_updated_at=(
                None
                if schedule_fee_rate_bps is not None or self.fee_rate_updated_at is None
                else _ensure_aware(self.fee_rate_updated_at)
            ),
            category=self.category,
            tags=_tuple_from_sequence(self.tags),
            matched_keywords=_tuple_from_sequence(self.matched_keywords),
            trading_status=TradingStatus(self.trading_status),
            reject_reason=self.reject_reason,
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
                idempotency_key=order.idempotency_key,
                reason=order.reason,
                post_only=order.post_only,
                raw_payload=payload,
            )

        if order.side is None or order.order_type is None:
            raise ValueError("OrderResult must include side/order_type to persist in orders table")

        order_key = _order_key(order)
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
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
            idempotency_key=order.intent.idempotency_key if order.intent and getattr(order.intent, "idempotency_key", None) else None,
            reason=order.reason,
            post_only=False if order.intent is None else getattr(order.intent, "post_only", False),
            raw_payload=payload,
        )

    def to_domain(self) -> Order:
        return Order(
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
    )

    @classmethod
    def from_domain(
        cls,
        fill: Fill,
        *,
        raw_payload: JsonMapping | None = None,
    ) -> "FillModel":
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
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
    """账户余额和买入闸门快照。"""

    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    balance_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    allowance_usdc: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
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
        Index("ix_account_snapshots_trace_account", "trace_id", "account_key"),
    )

    @classmethod
    def from_domain(
        cls,
        snapshot: AccountSnapshot,
        *,
        trace_id: str | None = None,
        raw_payload: JsonMapping | None = None,
        account_key: str = "primary",
    ) -> "AccountSnapshotModel":
        payload = _json_mapping(raw_payload) if raw_payload is not None else {
            "account_key": account_key,
            "trace_id": trace_id,
            "balance_usdc": str(snapshot.balance_usdc),
            "allowance_usdc": str(snapshot.allowance_usdc),
            "user_ws_connected": snapshot.user_ws_connected,
            "allow_new_entries": snapshot.allow_new_entries,
            "market_pauses": [pause.as_payload() for pause in snapshot.market_pauses],
            "last_reconcile_at": _json_safe(snapshot.last_reconcile_at),
        }
        return cls(
            account_key=account_key,
            trace_id=trace_id,
            balance_usdc=snapshot.balance_usdc,
            allowance_usdc=snapshot.allowance_usdc,
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


class AuditEventModel(Base, TimestampMixin):
    """审计事件表。

    记录脱敏后的事件 payload，便于复盘，但不作为交易真相来源。
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
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
        )

    def to_domain(self) -> AuditEvent:
        return AuditEvent(
            event_title=self.event_title,
            event_slug=self.event_slug,
            payload=dict(self.payload),
            trace_id=self.trace_id,
            created_at=self.created_at,
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
            idempotency_key=event.idempotency_key,
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
