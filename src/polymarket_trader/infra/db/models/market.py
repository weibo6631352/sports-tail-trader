from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _datetime_value,
    _decimal,
    _ensure_aware,
    _fee_rate_units_from_payload,
    _json_mapping,
    _json_safe,
    _text,
    _tuple_from_sequence,
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
    # 费率字段语义：NULL = 「尚未从 Polymarket API 拉到」（真实业务可选状态，非过渡列）。
    # market 创建在前、fee 采集在后；CLOB 异步上报 fee_rate_bps，未上报时保持 NULL。
    # 消费侧 (strategies/current/allocation.py) 显式处理 None：fee_rate_bps 为 None 时
    # 回退到 taker_base_fee_bps，仍为 None / <=0 则跳过 fee 调整。这是 §8 允许的真实可选
    # 语义而非「nullable 兼容列」。如果未来 fee 采集变成同步必备，再改 NOT NULL。
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
            "game_start_time": _json_safe(market.game_start_time),
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
        if market.game_start_time is not None:
            payload.setdefault("game_start_time", _json_safe(market.game_start_time))
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
            game_start_time=(
                _datetime_value(raw_payload.get("game_start_time"))
                or _datetime_value(raw_payload.get("gameStartTime"))
                or _datetime_value(raw_payload.get("gameStart"))
            ),
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


__all__ = ["MarketModel"]
