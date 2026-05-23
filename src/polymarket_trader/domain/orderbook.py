from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


# Polymarket CLOB 价格上下限（结算极端价位 = MM 兜底单，非真实双边盘）：
#  - bid 侧 ≥ CEILING_PRICE 表示 "MM 等接 SELL 的天花板单"，不是真买盘
#  - ask 侧 ≤ FLOOR_PRICE 表示 "MM 兜 BUY 的地板单"，不是真卖盘
# 与 ``infra/polymarket/market_ws_adapter.py`` 的 best_price/worst_ask 过滤阈值一致。
CEILING_PRICE = Decimal("0.99")
FLOOR_PRICE = Decimal("0.01")

# 真实可成交流动性的 USDC 下限。Polymarket min order 通常 ≥ $1，且我们的止盈/止损
# 单一般 ≥ $5。best 一档若不到 $5 USDC，下一笔 SELL 大概率穿到下一档——视为 dust。
DUST_USDC = Decimal("5")


class BidLiquidityState(StrEnum):
    """卖出（清仓）一侧的真实退出通道状态——直接决定能否挂 SELL。"""

    OK = "ok"                       # 有真买单 (best_bid < 0.99 且 size 充足)
    NO_BID = "no_bid"               # bids 完全空——没人接，挂 SELL 死等
    CEILING_ONLY = "ceiling_only"   # best_bid ≥ 0.99，只有 MM 接单（极端价）
    DUST_BID = "dust_bid"           # best 一档 USDC 太小，下一笔会穿档


class AskLiquidityState(StrEnum):
    """买入（开仓）一侧的真实成交通道状态——直接决定能否挂 BUY。"""

    OK = "ok"                       # 有真卖单 (best_ask > 0.01 且 size 充足)
    NO_ASK = "no_ask"               # asks 完全空——没人卖
    FLOOR_ONLY = "floor_only"       # best_ask ≤ 0.01，只有 MM 兜底
    DUST_ASK = "dust_ask"           # best 一档 USDC 太小


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

    def buyable_ask_depth(self, max_price: Decimal | None = None) -> Decimal:
        # 买入深度只看 ask 侧；调用方可按需传入价格上限做额外筛选。
        total = Decimal("0")
        for level in self.asks:
            if max_price is not None and level.price > max_price:
                continue
            total += level.size
        return total

    @property
    def bid_liquidity_state(self) -> BidLiquidityState:
        """卖出一侧的真实退出通道状态。

        判定顺序：完全空 → 天花板 → dust → ok。CEILING_ONLY 与 NO_BID 都意味着
        没有真实退出通道，挂 SELL 等于砸盘（被 MM $0.99 单秒吃）或死等。
        """
        if not self.bids or self.best_bid is None:
            return BidLiquidityState.NO_BID
        if self.best_bid >= CEILING_PRICE:
            return BidLiquidityState.CEILING_ONLY
        if self.best_bid_size is not None and self.best_bid * self.best_bid_size < DUST_USDC:
            return BidLiquidityState.DUST_BID
        return BidLiquidityState.OK

    @property
    def ask_liquidity_state(self) -> AskLiquidityState:
        """买入一侧的真实成交通道状态。"""
        if not self.asks or self.best_ask is None:
            return AskLiquidityState.NO_ASK
        if self.best_ask <= FLOOR_PRICE:
            return AskLiquidityState.FLOOR_ONLY
        if self.best_ask_size is not None and self.best_ask * self.best_ask_size < DUST_USDC:
            return AskLiquidityState.DUST_ASK
        return AskLiquidityState.OK

    @property
    def microprice(self) -> Decimal | None:
        """size 加权的"下一笔成交最可能价"。

        Cont-Stoikov microprice:
            microprice = (ask × bid_size + bid × ask_size) / (bid_size + ask_size)

        叉乘的直觉：bid 侧厚 → 薄的 ask 一侧先被吃穿 → 价格往 ask 拉。
        相比 mid = (bid+ask)/2，microprice 能反映"下一笔成交价"的真实期望。

        返回 None 的情形（无 fair value 可言）：
        - bid 侧 NO_BID / CEILING_ONLY：没人真接，没有 SELL 可参考价
        - ask 侧 NO_ASK / FLOOR_ONLY：没人真卖，没有 BUY 可参考价

        size 数据缺失时退化到 mid（已知 best 双边但不知道深度——保守用算术中价）。
        """
        if self.bid_liquidity_state in {BidLiquidityState.NO_BID, BidLiquidityState.CEILING_ONLY}:
            return None
        if self.ask_liquidity_state in {AskLiquidityState.NO_ASK, AskLiquidityState.FLOOR_ONLY}:
            return None
        if self.best_bid is None or self.best_ask is None:
            return None
        if self.best_bid_size is None or self.best_ask_size is None:
            return (self.best_bid + self.best_ask) / Decimal("2")
        total_size = self.best_bid_size + self.best_ask_size
        if total_size <= Decimal("0"):
            return (self.best_bid + self.best_ask) / Decimal("2")
        return (
            self.best_ask * self.best_bid_size + self.best_bid * self.best_ask_size
        ) / total_size

    @property
    def sell_actionable(self) -> bool:
        """是否能挂 SELL（持仓退出通道存在）。"""
        return self.bid_liquidity_state == BidLiquidityState.OK

    @property
    def buy_actionable(self) -> bool:
        """是否能挂 BUY（入场通道存在）。"""
        return self.ask_liquidity_state == AskLiquidityState.OK
