from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.order import Order, OrderSide
from polymarket_trader.domain.position import Position


class MarketPauseSource(StrEnum):
    MANUAL = "manual"
    RECONCILE = "reconcile"
    RISK = "risk"


class MarketPauseReason(StrEnum):
    MANUAL_PAUSE = "manual_pause"
    MARKET_NOT_TRADABLE = "market_not_tradable"
    MISSING_PRIMARY_OUTCOME = "missing_primary_outcome"
    UNEXPECTED_RESTING_ORDER = "unexpected_resting_order"


_RECOVERABLE_PAUSE_REASONS = frozenset(
    {
        MarketPauseReason.MARKET_NOT_TRADABLE.value,
        MarketPauseReason.MISSING_PRIMARY_OUTCOME.value,
    }
)


def _pause_reason_text(reason: MarketPauseReason | str) -> str:
    if isinstance(reason, MarketPauseReason):
        return reason.value
    return str(reason)


def _default_pause_source(reason: str) -> MarketPauseSource:
    if reason == MarketPauseReason.MANUAL_PAUSE.value:
        return MarketPauseSource.MANUAL
    if reason == MarketPauseReason.UNEXPECTED_RESTING_ORDER.value:
        return MarketPauseSource.RISK
    return MarketPauseSource.RECONCILE


@dataclass(frozen=True, slots=True)
class MarketPause:
    condition_id: str
    reason: str
    source: MarketPauseSource
    recoverable: bool

    @classmethod
    def build(
        cls,
        *,
        condition_id: str,
        reason: MarketPauseReason | str,
        source: MarketPauseSource | str | None = None,
        recoverable: bool | None = None,
    ) -> "MarketPause":
        reason_text = _pause_reason_text(reason)
        pause_source = _default_pause_source(reason_text) if source is None else MarketPauseSource(source)
        pause_recoverable = (
            pause_source == MarketPauseSource.RECONCILE
            and reason_text in _RECOVERABLE_PAUSE_REASONS
            if recoverable is None
            else recoverable
        )
        return cls(
            condition_id=condition_id,
            reason=reason_text,
            source=pause_source,
            recoverable=pause_recoverable,
        )

    def as_reason_pair(self) -> tuple[str, str]:
        return self.condition_id, self.reason

    def as_payload(self) -> dict[str, object]:
        return {
            "condition_id": self.condition_id,
            "reason": self.reason,
            "source": self.source.value,
            "recoverable": self.recoverable,
        }


@dataclass(frozen=True, slots=True)
class AccountHistoryPoint:
    """账户净值时间序列的一个采样点（查询结果 DTO）。"""

    recorded_at: datetime
    net_value_usdc: Decimal


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    balance_usdc: Decimal = Decimal("0")
    allowance_usdc: Decimal = Decimal("0")
    positions: tuple[Position, ...] = ()
    open_orders: tuple[Order, ...] = ()
    fills: tuple[Fill, ...] = ()
    user_ws_connected: bool = False
    allow_new_entries: bool = False
    market_pauses: tuple[MarketPause, ...] = ()
    last_reconcile_at: datetime | None = None

    @property
    def open_buy_reserved_usdc(self) -> Decimal:
        """返回交易所会为开放 BUY 订单预留的 USDC 金额。"""

        total = Decimal("0")
        for order in self.open_orders:
            if order.side != OrderSide.BUY or not order.open:
                continue
            total += _open_buy_order_reserved_usdc(order)
        return total

    @property
    def available_usdc(self) -> Decimal:
        available = min(self.balance_usdc, self.allowance_usdc) - self.open_buy_reserved_usdc
        if available < Decimal("0"):
            return Decimal("0")
        return available

    @property
    def equity_usdc(self) -> Decimal:
        """组合权益 = 可用 USDC + Σ(持仓真实可实现值)。

        position.current_value 来源链：reconcile worker 拉 data-api curPrice
        (last_trade) + worker 订阅触发用 best_bid mark-to-market。data-api 的
        cur_price 是 stale（冷盘几小时不动 + 可能反映对面 outcome 价），不能
        真实反映可成交价。

        关键：current_value=None 或 = cost_usdc 兜底都会 **虚假高估**——实测
        Kalinina case 显示 cv 是 stale 数据，best_bid=None 实际可实现 0。
        修复后：current_value 缺失 → 0（保守，没数据=没价值），不再用 cost 兜底。
        """

        total = self.available_usdc
        for position in self.positions:
            value = position.current_value
            if value is not None and value > Decimal("0"):
                total += value
        return total

    def get_position(self, condition_id: str, token_id: str) -> Position | None:
        for position in self.positions:
            if position.condition_id == condition_id and position.token_id == token_id:
                return position
        return None

    def pause_for_market(self, condition_id: str) -> MarketPause | None:
        for pause in self.market_pauses:
            if pause.condition_id == condition_id:
                return pause
        return None

    def is_market_paused(self, condition_id: str) -> bool:
        return self.pause_for_market(condition_id) is not None

    def without_market_pause(self, condition_id: str) -> "AccountSnapshot":
        return replace(
            self,
            market_pauses=tuple(
                pause
                for pause in self.market_pauses
                if pause.condition_id != condition_id
            ),
        )


    def open_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]:
        return tuple(
            order
            for order in self.open_orders
            if order.condition_id == condition_id and order.token_id == token_id
        )

    def open_buy_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]:
        return tuple(
            order
            for order in self.open_orders_for_market(condition_id, token_id)
            if order.side == OrderSide.BUY and order.open
        )

    def open_sell_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]:
        return tuple(
            order
            for order in self.open_orders_for_market(condition_id, token_id)
            if order.side == OrderSide.SELL and order.open
        )

    def open_sell_shares_for_market(self, condition_id: str, token_id: str) -> Decimal:
        total = Decimal("0")
        for order in self.open_sell_orders_for_market(condition_id, token_id):
            if order.remaining_shares is not None:
                total += order.remaining_shares
            elif order.size_shares is not None:
                total += order.size_shares
        return total


def _open_buy_order_reserved_usdc(order: Order) -> Decimal:
    """按 Polymarket CLOB 剩余 BUY 订单规模估算余额预留。"""

    if order.price is not None:
        shares = order.remaining_shares
        if shares is None and order.size_shares is not None:
            shares = order.size_shares - order.filled_shares
        if shares is not None:
            if shares <= Decimal("0"):
                return Decimal("0")
            return order.price * shares
    if order.amount_usdc is not None:
        return max(order.amount_usdc, Decimal("0"))
    if order.notional_usdc is not None:
        return max(order.notional_usdc, Decimal("0"))
    return Decimal("0")
