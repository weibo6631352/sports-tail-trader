"""跨 store 的层次化只读 view —— `EventView` / `MarketView` / `OutcomeView`。

底层 4 个扁平 store（market_registry / orderbook_history_buffer /
account_state_store / market_metadata_store）保持独立锁；本模块只定义视图 DTO，
由 `runtime/data_graph.py` 在 P0 路径上即时聚合构造。view 是 frozen dataclass，
无 IO，引用底层 DTO（不深拷贝），构造成本 = 几次 dict.get + tuple()。

一对多关系（Event → Market → Outcome）通过嵌套 tuple 显式表达，调用方读完整图
不再需要跨 store 拼接。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket_trader.domain.account import MarketPause
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.market_metadata import EntryMetadataRecord
from polymarket_trader.domain.order import Order, OrderSide
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position


@dataclass(frozen=True, slots=True)
class OutcomeView:
    """单 outcome (token) 维度聚合——盘口 + 持仓 + 该 token 的开放挂单。"""

    token_id: str
    outcome: str
    orderbook: OrderbookSnapshot | None
    position: Position | None
    open_orders: tuple[Order, ...]

    @property
    def shares(self) -> Decimal:
        return self.position.shares if self.position is not None else Decimal("0")

    @property
    def has_position(self) -> bool:
        return self.position is not None and self.position.shares > Decimal("0")

    @property
    def best_bid(self) -> Decimal | None:
        return self.orderbook.best_bid if self.orderbook is not None else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.orderbook.best_ask if self.orderbook is not None else None

    @property
    def open_buy_orders(self) -> tuple[Order, ...]:
        return tuple(o for o in self.open_orders if o.side == OrderSide.BUY and o.open)

    @property
    def open_sell_orders(self) -> tuple[Order, ...]:
        return tuple(o for o in self.open_orders if o.side == OrderSide.SELL and o.open)

    @property
    def has_open_buy(self) -> bool:
        return any(o.side == OrderSide.BUY and o.open for o in self.open_orders)

    @property
    def has_open_sell(self) -> bool:
        return any(o.side == OrderSide.SELL and o.open for o in self.open_orders)


@dataclass(frozen=True, slots=True)
class MarketView:
    """单 market (condition) 维度聚合——含 N 个 outcome（通常 2）。"""

    condition_id: str
    market: Market
    outcomes: tuple[OutcomeView, ...]
    metadata: EntryMetadataRecord | None
    pause: MarketPause | None

    @property
    def market_slug(self) -> str:
        return self.market.market_slug

    @property
    def event_slug(self) -> str | None:
        return self.market.event_slug

    @property
    def event_title(self) -> str | None:
        return self.market.event_title

    @property
    def is_paused(self) -> bool:
        return self.pause is not None

    @property
    def total_shares(self) -> Decimal:
        total = Decimal("0")
        for outcome in self.outcomes:
            if outcome.position is not None:
                total += outcome.position.shares
        return total

    @property
    def total_position_usdc(self) -> Decimal:
        """所有 outcome 持仓估值之和（cur_price 优先，否则 best_bid 兜底）。

        与 `Position.current_value` 同语义——缺值不用 cost 兜底（避免虚假高估，参
        [account.py](src/polymarket_trader/domain/account.py) `equity_usdc` 说明）。
        """

        total = Decimal("0")
        for outcome in self.outcomes:
            if outcome.position is None or outcome.position.shares <= Decimal("0"):
                continue
            price = outcome.position.cur_price
            if price is None:
                price = outcome.best_bid
            if price is None or price <= Decimal("0"):
                continue
            total += outcome.position.shares * price
        return total

    @property
    def has_any_position(self) -> bool:
        return any(o.has_position for o in self.outcomes)

    def outcome_for(self, token_id: str) -> OutcomeView | None:
        for outcome in self.outcomes:
            if outcome.token_id == token_id:
                return outcome
        return None


@dataclass(frozen=True, slots=True)
class EventView:
    """单 event 维度聚合——含 N 个 market（同 event_slug，如 ML/Totals/Spreads/分局 prop）。"""

    event_slug: str
    markets: tuple[MarketView, ...]

    @property
    def event_title(self) -> str | None:
        for market in self.markets:
            if market.event_title:
                return market.event_title
        return None

    @property
    def total_position_usdc(self) -> Decimal:
        total = Decimal("0")
        for market in self.markets:
            total += market.total_position_usdc
        return total

    @property
    def has_any_position(self) -> bool:
        return any(m.has_any_position for m in self.markets)

    def market_for(self, condition_id: str) -> MarketView | None:
        for market in self.markets:
            if market.condition_id == condition_id:
                return market
        return None
