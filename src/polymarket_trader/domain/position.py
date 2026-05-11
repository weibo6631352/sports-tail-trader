from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Position:
    # strategy_id 必填，无默认值。框架/策略边界处必须显式提供；缺失直接抛错。
    strategy_id: str
    condition_id: str
    token_id: str
    shares: Decimal
    cost_usdc: Decimal
    market_slug: str | None = None
    open_buy_shares: Decimal = Decimal("0")
    open_sell_shares: Decimal = Decimal("0")
    pending_buy_shares: Decimal = Decimal("0")
    confirmed_shares: Decimal = Decimal("0")
    last_order_id: str | None = None
    last_trade_id: str | None = None
    confirmation_status: str = "unknown"
    updated_at: datetime | None = None
    avg_price: Decimal | None = None
    initial_value: Decimal | None = None
    current_value: Decimal | None = None
    cash_pnl: Decimal | None = None
    percent_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    percent_realized_pnl: Decimal | None = None
    cur_price: Decimal | None = None
    redeemable: bool | None = None

    @property
    def settled_zero_value(self) -> bool:
        """已结算且当前价值为 0 的仓位不再代表可交易资金暴露。"""

        if self.redeemable is not True:
            return False
        if self.current_value != Decimal("0"):
            return False
        if self.cur_price is not None and self.cur_price != Decimal("0"):
            return False
        if self.cash_pnl is not None and self.cash_pnl > Decimal("0"):
            return False
        return True

    def with_open_buy_shares(self, shares: Decimal) -> "Position":
        return replace(self, open_buy_shares=shares)

    def with_open_sell_shares(self, shares: Decimal) -> "Position":
        return replace(self, open_sell_shares=shares)

    def with_pending_buy_shares(self, shares: Decimal) -> "Position":
        return replace(self, pending_buy_shares=shares)

    def with_confirmation(
        self,
        *,
        confirmed_shares: Decimal,
        confirmation_status: str,
        last_order_id: str | None = None,
        last_trade_id: str | None = None,
        updated_at: datetime | None = None,
    ) -> "Position":
        return replace(
            self,
            confirmed_shares=confirmed_shares,
            confirmation_status=confirmation_status,
            last_order_id=self.last_order_id if last_order_id is None else last_order_id,
            last_trade_id=self.last_trade_id if last_trade_id is None else last_trade_id,
            updated_at=updated_at or self.updated_at,
        )
