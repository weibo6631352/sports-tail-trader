"""真实订单簿 level-by-level 撮合（taker-only）。

给定 OrderbookSnapshot，模拟一个 taker 订单逐档消耗对手盘，输出加权均价、
实际成交份额、未成交剩余量与每档消耗记录。沙箱专用，不参与真实交易路径。

撮合假设：
- 假设撮合完全消费当前 ask/bid 队列（不模拟队列位置 / 同步对手撤单）
- 不模拟自挂限价单成为 maker 被对手吃的概率（taker-only）
- limit_price 严格按"BUY 不超过 / SELL 不低于"截断
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket_trader.domain.orderbook import OrderbookSnapshot

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ConsumedLevel:
    """单档消耗记录，便于审计成交细节。"""

    price: Decimal
    shares: Decimal
    notional_usdc: Decimal


@dataclass(frozen=True, slots=True)
class MatchResult:
    """撮合结果。BUY 用 ``unfilled_amount_usdc``；SELL 用 ``unfilled_size_shares``。"""

    filled_shares: Decimal
    avg_price: Decimal | None
    gross_notional_usdc: Decimal
    consumed_levels: tuple[ConsumedLevel, ...]
    unfilled_amount_usdc: Decimal = _ZERO
    unfilled_size_shares: Decimal = _ZERO

    @property
    def is_filled(self) -> bool:
        return self.filled_shares > _ZERO

    @property
    def is_full_fill(self) -> bool:
        return (
            self.is_filled
            and self.unfilled_amount_usdc <= _ZERO
            and self.unfilled_size_shares <= _ZERO
        )


def match_taker_buy(
    orderbook: OrderbookSnapshot | None,
    amount_usdc: Decimal,
    limit_price: Decimal | None,
) -> MatchResult:
    """模拟 taker BUY：吃 ask 队列，按价格升序消耗。

    ``limit_price`` 为 None 表示市价（不截断）；否则严格不允许吃高于该价的档位。
    """

    if amount_usdc <= _ZERO or orderbook is None or not orderbook.asks:
        return MatchResult(
            filled_shares=_ZERO,
            avg_price=None,
            gross_notional_usdc=_ZERO,
            consumed_levels=(),
            unfilled_amount_usdc=max(amount_usdc, _ZERO),
        )

    remaining_amount = amount_usdc
    filled_shares = _ZERO
    spent_usdc = _ZERO
    consumed: list[ConsumedLevel] = []

    for level in sorted(orderbook.asks, key=lambda lvl: lvl.price):
        if limit_price is not None and level.price > limit_price:
            break
        if level.price <= _ZERO or level.size <= _ZERO:
            continue
        max_shares_by_amount = remaining_amount / level.price
        shares_at_level = min(level.size, max_shares_by_amount)
        if shares_at_level <= _ZERO:
            break
        notional = shares_at_level * level.price
        filled_shares += shares_at_level
        spent_usdc += notional
        remaining_amount -= notional
        consumed.append(ConsumedLevel(price=level.price, shares=shares_at_level, notional_usdc=notional))
        if remaining_amount <= _ZERO:
            remaining_amount = _ZERO
            break

    avg_price = (spent_usdc / filled_shares) if filled_shares > _ZERO else None
    return MatchResult(
        filled_shares=filled_shares,
        avg_price=avg_price,
        gross_notional_usdc=spent_usdc,
        consumed_levels=tuple(consumed),
        unfilled_amount_usdc=max(remaining_amount, _ZERO),
    )


def match_taker_sell(
    orderbook: OrderbookSnapshot | None,
    size_shares: Decimal,
    limit_price: Decimal | None,
) -> MatchResult:
    """模拟 taker SELL：吃 bid 队列，按价格降序消耗。

    ``limit_price`` 为 None 表示市价（不截断）；否则严格不允许吃低于该价的档位。
    """

    if size_shares <= _ZERO or orderbook is None or not orderbook.bids:
        return MatchResult(
            filled_shares=_ZERO,
            avg_price=None,
            gross_notional_usdc=_ZERO,
            consumed_levels=(),
            unfilled_size_shares=max(size_shares, _ZERO),
        )

    remaining_size = size_shares
    filled_shares = _ZERO
    received_usdc = _ZERO
    consumed: list[ConsumedLevel] = []

    for level in sorted(orderbook.bids, key=lambda lvl: lvl.price, reverse=True):
        if limit_price is not None and level.price < limit_price:
            break
        if level.price <= _ZERO or level.size <= _ZERO:
            continue
        shares_at_level = min(level.size, remaining_size)
        if shares_at_level <= _ZERO:
            break
        notional = shares_at_level * level.price
        filled_shares += shares_at_level
        received_usdc += notional
        remaining_size -= shares_at_level
        consumed.append(ConsumedLevel(price=level.price, shares=shares_at_level, notional_usdc=notional))
        if remaining_size <= _ZERO:
            remaining_size = _ZERO
            break

    avg_price = (received_usdc / filled_shares) if filled_shares > _ZERO else None
    return MatchResult(
        filled_shares=filled_shares,
        avg_price=avg_price,
        gross_notional_usdc=received_usdc,
        consumed_levels=tuple(consumed),
        unfilled_size_shares=max(remaining_size, _ZERO),
    )
