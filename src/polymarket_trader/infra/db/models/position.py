from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polymarket_trader.domain.position import Position
from polymarket_trader.infra.db.base import (
    Base,
    JsonMapping,
    TimestampMixin,
    _decimal,
    _json_mapping,
)


class PositionModel(Base, TimestampMixin):
    """持仓热状态和恢复参考。"""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(255), index=True)
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


__all__ = ["PositionModel"]
