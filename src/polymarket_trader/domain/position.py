from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Position:
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
