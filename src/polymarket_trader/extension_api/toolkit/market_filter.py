"""市场过滤 DSL：策略侧不再写散落的 if/else 链。

链式 API，所有条件 AND 组合：
    >>> dsl = MarketFilterDSL().slug_contains("nba").time_to_resolution_at_most(timedelta(hours=2))
    >>> dsl.evaluate(market, orderbook)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.orderbook import OrderbookSnapshot

_Predicate = Callable[[Market, OrderbookSnapshot | None], bool]


@dataclass(frozen=True, slots=True)
class MarketFilterDSL:
    """不可变 DSL；每次 ``with_*`` 返回新实例。"""

    predicates: tuple[_Predicate, ...] = field(default_factory=tuple)

    def _with(self, predicate: _Predicate) -> "MarketFilterDSL":
        return MarketFilterDSL(predicates=self.predicates + (predicate,))

    def slug_contains(self, *needles: str) -> "MarketFilterDSL":
        lowered = tuple(n.lower() for n in needles if n)

        def predicate(market: Market, _orderbook: OrderbookSnapshot | None) -> bool:
            slug = (market.market_slug or "").lower()
            return any(needle in slug for needle in lowered)

        return self._with(predicate)

    def slug_excludes(self, *needles: str) -> "MarketFilterDSL":
        lowered = tuple(n.lower() for n in needles if n)

        def predicate(market: Market, _orderbook: OrderbookSnapshot | None) -> bool:
            slug = (market.market_slug or "").lower()
            return all(needle not in slug for needle in lowered)

        return self._with(predicate)

    def category_in(self, *categories: str) -> "MarketFilterDSL":
        lowered = {c.lower() for c in categories if c}

        def predicate(market: Market, _orderbook: OrderbookSnapshot | None) -> bool:
            return (market.category or "").lower() in lowered

        return self._with(predicate)

    def time_to_resolution_at_most(self, delta: timedelta) -> "MarketFilterDSL":
        def predicate(market: Market, _orderbook: OrderbookSnapshot | None) -> bool:
            if market.end_date is None:
                return False
            now = datetime.now(timezone.utc)
            end = market.end_date if market.end_date.tzinfo else market.end_date.replace(tzinfo=timezone.utc)
            return (end - now) <= delta

        return self._with(predicate)

    def time_to_resolution_at_least(self, delta: timedelta) -> "MarketFilterDSL":
        def predicate(market: Market, _orderbook: OrderbookSnapshot | None) -> bool:
            if market.end_date is None:
                return False
            now = datetime.now(timezone.utc)
            end = market.end_date if market.end_date.tzinfo else market.end_date.replace(tzinfo=timezone.utc)
            return (end - now) >= delta

        return self._with(predicate)

    def best_ask_at_most(self, price: Decimal) -> "MarketFilterDSL":
        def predicate(_market: Market, orderbook: OrderbookSnapshot | None) -> bool:
            if orderbook is None or orderbook.best_ask is None:
                return False
            return orderbook.best_ask <= price

        return self._with(predicate)

    def best_ask_at_least(self, price: Decimal) -> "MarketFilterDSL":
        def predicate(_market: Market, orderbook: OrderbookSnapshot | None) -> bool:
            if orderbook is None or orderbook.best_ask is None:
                return False
            return orderbook.best_ask >= price

        return self._with(predicate)

    def buyable_depth_at_least(self, depth_usdc: Decimal, *, max_price: Decimal | None = None) -> "MarketFilterDSL":
        def predicate(_market: Market, orderbook: OrderbookSnapshot | None) -> bool:
            if orderbook is None:
                return False
            total = Decimal("0")
            for level in orderbook.asks:
                if max_price is not None and level.price > max_price:
                    continue
                total += level.price * level.size
                if total >= depth_usdc:
                    return True
            return False

        return self._with(predicate)

    def custom(self, predicate: _Predicate) -> "MarketFilterDSL":
        return self._with(predicate)

    def evaluate(self, market: Market, orderbook: OrderbookSnapshot | None = None) -> bool:
        return all(p(market, orderbook) for p in self.predicates)
