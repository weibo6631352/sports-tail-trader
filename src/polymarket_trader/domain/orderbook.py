from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderbookSnapshot:
    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    received_at: datetime
    market_slug: str | None = None
    condition_id: str | None = None
    best_bid_size: Decimal | None = None
    best_ask_size: Decimal | None = None
    last_trade_price: Decimal | None = None
    tick_size: Decimal | None = None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def snapshot_time(self) -> datetime:
        # 交易路径里常把 received_at 当成快照时间；这里保留一个语义更直白的只读别名。
        return self.received_at

    def buyable_ask_depth(self, max_price: Decimal | None = None) -> Decimal:
        # 买入深度只看 ask 侧；调用方可按需传入价格上限做额外筛选。
        total = Decimal("0")
        for level in self.asks:
            if max_price is not None and level.price > max_price:
                continue
            total += level.size
        return total
