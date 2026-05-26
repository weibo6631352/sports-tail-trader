"""Orderbook 计算工具：高频复用的盘口计算。

这些函数都返回 ``Decimal | None``——盘口缺侧时返回 None，调用方自行决定降级路径，
不在工具函数里默默替换成 0。
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.orderbook import OrderbookSnapshot


def midpoint(orderbook: OrderbookSnapshot | None) -> Decimal | None:
    """返回 (best_bid + best_ask) / 2；任一侧缺失返回 None。"""

    if orderbook is None or orderbook.best_bid is None or orderbook.best_ask is None:
        return None
    return (orderbook.best_bid + orderbook.best_ask) / Decimal("2")


def spread_bps(orderbook: OrderbookSnapshot | None) -> Decimal | None:
    """返回价差占 midpoint 的 basis points。任一侧缺失或 midpoint 为 0 返回 None。"""

    mid = midpoint(orderbook)
    if mid is None or mid == 0 or orderbook is None or orderbook.best_bid is None or orderbook.best_ask is None:
        return None
    return ((orderbook.best_ask - orderbook.best_bid) / mid) * Decimal("10000")


def depth_at_price(
    orderbook: OrderbookSnapshot | None,
    *,
    side: str,
    price: Decimal,
) -> Decimal:
    """返回指定 side（"bid"/"ask"）在 ``price`` 价位上的累计 size。

    ask 侧统计 price 及以下（愿意吃的最高价）；bid 侧统计 price 及以上。
    缺盘口时返回 0。
    """

    if orderbook is None:
        return Decimal("0")
    levels = orderbook.asks if side == "ask" else orderbook.bids if side == "bid" else ()
    total = Decimal("0")
    for level in levels:
        if side == "ask" and level.price <= price:
            total += level.size
        elif side == "bid" and level.price >= price:
            total += level.size
    return total


def depth_weighted_price(
    orderbook: OrderbookSnapshot | None,
    *,
    side: str,
    target_size: Decimal,
) -> Decimal | None:
    """返回吃掉 target_size 所需的加权平均价格。

    side="ask" 为 BUY 视角（按 ask 升序累计）；side="bid" 为 SELL 视角（按 bid 降序）。
    深度不足时返回 None，调用方据此降级。
    """

    if orderbook is None or target_size <= 0:
        return None
    levels = orderbook.asks if side == "ask" else orderbook.bids if side == "bid" else ()
    if not levels:
        return None
    accumulated_size = Decimal("0")
    accumulated_notional = Decimal("0")
    for level in levels:
        take = min(level.size, target_size - accumulated_size)
        accumulated_size += take
        accumulated_notional += take * level.price
        if accumulated_size >= target_size:
            break
    if accumulated_size < target_size:
        return None
    return accumulated_notional / target_size
