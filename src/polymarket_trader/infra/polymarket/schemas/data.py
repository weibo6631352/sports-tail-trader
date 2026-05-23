"""Data API（positions / balance & allowance）schema 层。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from polymarket_trader.domain.position import Position
from polymarket_trader.infra.polymarket.base_client import _summary

from ._helpers import (
    _coerce_allowance_usdc,
    _coerce_bool,
    _coerce_collateral_usdc,
    _coerce_datetime,
    _coerce_decimal,
    _coerce_int,
    _first_text,
    _first_value,
    _unwrap_mapping,
    _utc_now,
)


@dataclass(frozen=True, slots=True)
class BalanceAllowanceDTO:
    raw: Mapping[str, Any]
    balance_usdc: Decimal = Decimal("0")
    allowance_usdc: Decimal = Decimal("0")
    updated_at: datetime = field(default_factory=_utc_now)
    raw_summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "balance_usdc",
            self.balance_usdc
            if self.balance_usdc is not None
            else _coerce_decimal(_first_value(self.raw, "balance")) or Decimal("0"),
        )
        object.__setattr__(
            self,
            "allowance_usdc",
            self.allowance_usdc
            if self.allowance_usdc is not None
            else _coerce_decimal(_first_value(self.raw, "allowance")) or Decimal("0"),
        )
        updated_at = _coerce_datetime(_first_value(self.raw, "updated_at", "updatedAt", "timestamp"))
        if updated_at is not None:
            object.__setattr__(self, "updated_at", updated_at)
        summary = self.raw_summary.strip() if self.raw_summary else ""
        if not summary:
            summary = _summary(self.raw) or ""
        object.__setattr__(self, "raw_summary", summary)


@dataclass(frozen=True, slots=True)
class DataPositionDTO:
    raw: Mapping[str, Any]
    condition_id: str
    token_id: str
    shares: Decimal
    cost_usdc: Decimal
    market_slug: str | None = None
    proxy_wallet: str | None = None
    open_buy_shares: Decimal = Decimal("0")
    open_sell_shares: Decimal = Decimal("0")
    pending_buy_shares: Decimal = Decimal("0")
    confirmed_shares: Decimal = Decimal("0")
    last_order_id: str | None = None
    last_trade_id: str | None = None
    confirmation_status: str = "unknown"
    updated_at: datetime = field(default_factory=_utc_now)
    avg_price: Decimal | None = None
    initial_value: Decimal | None = None
    current_value: Decimal | None = None
    cash_pnl: Decimal | None = None
    percent_pnl: Decimal | None = None
    total_bought: Decimal | None = None
    realized_pnl: Decimal | None = None
    percent_realized_pnl: Decimal | None = None
    cur_price: Decimal | None = None
    redeemable: bool | None = None
    mergeable: bool | None = None
    title: str | None = None
    icon: str | None = None
    event_slug: str | None = None
    outcome: str | None = None
    outcome_index: int | None = None
    opposite_outcome: str | None = None
    opposite_asset: str | None = None
    end_date: datetime | None = None
    negative_risk: bool | None = None
    raw_summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "condition_id", str(self.condition_id).strip())
        object.__setattr__(self, "token_id", str(self.token_id).strip())
        object.__setattr__(self, "market_slug", self.market_slug or _first_text(self.raw, "market_slug", "marketSlug", "slug"))
        object.__setattr__(self, "proxy_wallet", self.proxy_wallet or _first_text(self.raw, "proxyWallet", "proxy_wallet"))
        object.__setattr__(self, "shares", self.shares if self.shares is not None else _coerce_decimal(_first_value(self.raw, "shares", "size", "quantity")) or Decimal("0"))
        cost_usdc = self.cost_usdc
        if cost_usdc is None:
            cost_usdc = _coerce_decimal(_first_value(self.raw, "cost_usdc", "cost", "value", "initialValue"))
        if cost_usdc is None:
            avg_price = _coerce_decimal(_first_value(self.raw, "avg_price", "avgPrice"))
            if avg_price is not None:
                cost_usdc = avg_price * self.shares
        object.__setattr__(self, "cost_usdc", cost_usdc or Decimal("0"))
        object.__setattr__(self, "open_buy_shares", self.open_buy_shares if self.open_buy_shares is not None else _coerce_decimal(_first_value(self.raw, "open_buy_shares", "openBuyShares")) or Decimal("0"))
        object.__setattr__(self, "open_sell_shares", self.open_sell_shares if self.open_sell_shares is not None else _coerce_decimal(_first_value(self.raw, "open_sell_shares", "openSellShares")) or Decimal("0"))
        object.__setattr__(self, "pending_buy_shares", self.pending_buy_shares if self.pending_buy_shares is not None else _coerce_decimal(_first_value(self.raw, "pending_buy_shares", "pendingBuyShares")) or Decimal("0"))
        object.__setattr__(self, "confirmed_shares", self.confirmed_shares if self.confirmed_shares is not None else _coerce_decimal(_first_value(self.raw, "confirmed_shares", "confirmedShares")) or Decimal("0"))
        object.__setattr__(self, "last_order_id", self.last_order_id or _first_text(self.raw, "last_order_id", "lastOrderId"))
        object.__setattr__(self, "last_trade_id", self.last_trade_id or _first_text(self.raw, "last_trade_id", "lastTradeId"))
        object.__setattr__(self, "confirmation_status", self.confirmation_status or _first_text(self.raw, "confirmation_status", "confirmationStatus") or "unknown")
        updated_at = _coerce_datetime(_first_value(self.raw, "updated_at", "updatedAt", "timestamp"))
        if updated_at is not None:
            object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "avg_price", self.avg_price if self.avg_price is not None else _coerce_decimal(_first_value(self.raw, "avg_price", "avgPrice")))
        object.__setattr__(self, "initial_value", self.initial_value if self.initial_value is not None else _coerce_decimal(_first_value(self.raw, "initialValue", "initial_value")))
        object.__setattr__(self, "current_value", self.current_value if self.current_value is not None else _coerce_decimal(_first_value(self.raw, "currentValue", "current_value")))
        object.__setattr__(self, "cash_pnl", self.cash_pnl if self.cash_pnl is not None else _coerce_decimal(_first_value(self.raw, "cashPnl", "cash_pnl")))
        object.__setattr__(self, "percent_pnl", self.percent_pnl if self.percent_pnl is not None else _coerce_decimal(_first_value(self.raw, "percentPnl", "percent_pnl")))
        object.__setattr__(self, "total_bought", self.total_bought if self.total_bought is not None else _coerce_decimal(_first_value(self.raw, "totalBought", "total_bought")))
        object.__setattr__(self, "realized_pnl", self.realized_pnl if self.realized_pnl is not None else _coerce_decimal(_first_value(self.raw, "realizedPnl", "realized_pnl")))
        object.__setattr__(
            self,
            "percent_realized_pnl",
            self.percent_realized_pnl
            if self.percent_realized_pnl is not None
            else _coerce_decimal(_first_value(self.raw, "percentRealizedPnl", "percent_realized_pnl")),
        )
        object.__setattr__(self, "cur_price", self.cur_price if self.cur_price is not None else _coerce_decimal(_first_value(self.raw, "curPrice", "cur_price")))
        object.__setattr__(self, "redeemable", self.redeemable if self.redeemable is not None else _coerce_bool(_first_value(self.raw, "redeemable")))
        object.__setattr__(self, "mergeable", self.mergeable if self.mergeable is not None else _coerce_bool(_first_value(self.raw, "mergeable")))
        object.__setattr__(self, "title", self.title or _first_text(self.raw, "title"))
        object.__setattr__(self, "icon", self.icon or _first_text(self.raw, "icon"))
        object.__setattr__(self, "event_slug", self.event_slug or _first_text(self.raw, "eventSlug", "event_slug"))
        object.__setattr__(self, "outcome", self.outcome or _first_text(self.raw, "outcome"))
        object.__setattr__(
            self,
            "outcome_index",
            self.outcome_index
            if self.outcome_index is not None
            else _coerce_int(_first_value(self.raw, "outcomeIndex", "outcome_index")),
        )
        object.__setattr__(self, "opposite_outcome", self.opposite_outcome or _first_text(self.raw, "oppositeOutcome", "opposite_outcome"))
        object.__setattr__(self, "opposite_asset", self.opposite_asset or _first_text(self.raw, "oppositeAsset", "opposite_asset"))
        object.__setattr__(
            self,
            "end_date",
            self.end_date if self.end_date is not None else _coerce_datetime(_first_value(self.raw, "endDate", "end_date")),
        )
        object.__setattr__(
            self,
            "negative_risk",
            self.negative_risk
            if self.negative_risk is not None
            else _coerce_bool(_first_value(self.raw, "negativeRisk", "negative_risk")),
        )
        summary = self.raw_summary.strip() if self.raw_summary else ""
        if not summary:
            summary = _summary(self.raw) or ""
        object.__setattr__(self, "raw_summary", summary)

    def to_position(self, *, strategy_id: str) -> Position:
        """Data-api 持仓 → 内部 Position。

        **关键：mark-to-market 字段（current_value / cur_price / cash_pnl /
        percent_pnl）一律设 None，不从 data-api 写。** Polymarket data-api 返回
        的 currentValue / curPrice 是缓存值（last_trade 或 stale settlement
        computation），不反映当前 best_bid——会让 worker MTM 修正（cv=0 when
        no_bid）被 reconcile 静默覆盖（task #35 根因）。

        交易主链路上的 MTM 真相源唯一：worker `_handle_orderbook_snapshot_updated`
        基于实时 orderbook + sell_actionable 守门写 current_value。reconcile 只
        提供"账户事实"字段（shares / cost / 已结算 realized_pnl / avg_price），
        不参与"当前可实现价值"计算。
        """
        if not strategy_id:
            raise ValueError("to_position requires non-empty strategy_id")
        return Position(
            strategy_id=strategy_id,
            condition_id=self.condition_id,
            token_id=self.token_id,
            shares=self.shares,
            cost_usdc=self.cost_usdc,
            market_slug=self.market_slug,
            open_buy_shares=self.open_buy_shares,
            open_sell_shares=self.open_sell_shares,
            pending_buy_shares=self.pending_buy_shares,
            confirmed_shares=self.confirmed_shares,
            last_order_id=self.last_order_id,
            last_trade_id=self.last_trade_id,
            confirmation_status=self.confirmation_status,
            updated_at=self.updated_at,
            avg_price=self.avg_price,
            initial_value=self.initial_value,
            # mark-to-market 字段不从 data-api 写（避免 stale curPrice 覆盖 worker MTM）
            current_value=None,
            cash_pnl=None,
            percent_pnl=None,
            cur_price=None,
            realized_pnl=self.realized_pnl,
            percent_realized_pnl=self.percent_realized_pnl,
            redeemable=self.redeemable,
        )


def normalize_position_payload(payload: Mapping[str, Any]) -> DataPositionDTO:
    normalized = _unwrap_mapping(payload)
    shares = _coerce_decimal(_first_value(normalized, "shares", "size", "quantity")) or Decimal("0")
    cost_usdc = _coerce_decimal(_first_value(normalized, "cost_usdc", "cost", "value", "initialValue"))
    if cost_usdc is None:
        avg_price = _coerce_decimal(_first_value(normalized, "avg_price", "avgPrice"))
        if avg_price is not None:
            cost_usdc = avg_price * shares
    return DataPositionDTO(
        raw=normalized,
        condition_id=_first_text(normalized, "condition_id", "conditionId", "condition") or "",
        token_id=_first_text(normalized, "token_id", "tokenId", "asset_id", "assetId", "asset") or "",
        shares=shares,
        cost_usdc=cost_usdc or Decimal("0"),
        market_slug=_first_text(normalized, "market_slug", "marketSlug", "slug"),
        open_buy_shares=_coerce_decimal(_first_value(normalized, "open_buy_shares", "openBuyShares")) or Decimal("0"),
        open_sell_shares=_coerce_decimal(_first_value(normalized, "open_sell_shares", "openSellShares")) or Decimal("0"),
        pending_buy_shares=_coerce_decimal(_first_value(normalized, "pending_buy_shares", "pendingBuyShares")) or Decimal("0"),
        confirmed_shares=_coerce_decimal(_first_value(normalized, "confirmed_shares", "confirmedShares")) or Decimal("0"),
        last_order_id=_first_text(normalized, "last_order_id", "lastOrderId"),
        last_trade_id=_first_text(normalized, "last_trade_id", "lastTradeId"),
        confirmation_status=_first_text(normalized, "confirmation_status", "confirmationStatus") or "unknown",
        updated_at=_coerce_datetime(_first_value(normalized, "updated_at", "updatedAt")) or _utc_now(),
        avg_price=_coerce_decimal(_first_value(normalized, "avg_price", "avgPrice")),
        initial_value=_coerce_decimal(_first_value(normalized, "initialValue", "initial_value")),
        current_value=_coerce_decimal(_first_value(normalized, "currentValue", "current_value")),
        cash_pnl=_coerce_decimal(_first_value(normalized, "cashPnl", "cash_pnl")),
        percent_pnl=_coerce_decimal(_first_value(normalized, "percentPnl", "percent_pnl")),
        realized_pnl=_coerce_decimal(_first_value(normalized, "realizedPnl", "realized_pnl")),
        percent_realized_pnl=_coerce_decimal(
            _first_value(normalized, "percentRealizedPnl", "percent_realized_pnl")
        ),
        cur_price=_coerce_decimal(_first_value(normalized, "curPrice", "cur_price")),
        redeemable=_coerce_bool(_first_value(normalized, "redeemable")),
    )


def normalize_balance_allowance_payload(payload: Mapping[str, Any]) -> BalanceAllowanceDTO:
    normalized = _unwrap_mapping(payload)
    balance = _coerce_collateral_usdc(_first_value(normalized, "balance"))
    allowance = _coerce_allowance_usdc(normalized)
    return BalanceAllowanceDTO(
        raw=normalized,
        balance_usdc=balance or Decimal("0"),
        allowance_usdc=allowance or Decimal("0"),
        updated_at=_coerce_datetime(_first_value(normalized, "updated_at", "updatedAt")) or _utc_now(),
    )


def data_position_to_domain_position(payload: Mapping[str, Any], *, strategy_id: str) -> Position:
    return normalize_position_payload(payload).to_position(strategy_id=strategy_id)
