"""通用辅助：从 ExtensionContext.metadata 中读 Decimal / 文本；Fill 金额计算。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.extension_api import ExtensionContext


def bid_plus_tick_fallback_ask(
    orderbook: OrderbookSnapshot | None,
    tick_size: Decimal | None,
) -> Decimal | None:
    """missing_best_ask 时用 ``best_bid + tick_size`` 估算 fallback ask。

    返回 None 表示 fallback 不可用（无 bid / 无 tick / 估算价越界）。返回估算价时
    调用方应把执行权限降级为 RECORD_ONLY——估算价只让 evaluator 跑出 fair_value
    与"理论可成交价"的对比，下单仍需要真实 ask 流动性。

    覆盖 outright + tail 两条评估路径——missing_best_ask 是单场盘口最大占比
    拒绝原因（实测 single_game 96%+），让两条路径都能用同一份 fallback 语义。
    """

    if orderbook is None or tick_size is None or tick_size <= Decimal("0"):
        return None
    best_bid = orderbook.best_bid
    if best_bid is None or best_bid <= Decimal("0"):
        return None
    fallback_ask = best_bid + tick_size
    if fallback_ask <= Decimal("0") or fallback_ask >= Decimal("1"):
        return None
    return fallback_ask


def bid_plus_tick_fallback_metadata(
    *,
    orderbook: OrderbookSnapshot,
    tick_size: Decimal,
    fallback_ask: Decimal,
) -> dict[str, str]:
    """统一 outright + tail 两条路径 RECORD_ONLY decision 的 fallback metadata。

    调用方已通过 ``bid_plus_tick_fallback_ask`` 拿到非 None 的 ``fallback_ask``，
    这里只负责拼成 decision_records 用于事后校准的四字段写入块——抽出来避免
    两个 callsite 字段名 / 字符串化方式漂移。
    """

    return {
        "best_ask_fallback": "bid_plus_tick",
        "best_bid": str(orderbook.best_bid),
        "tick_size": str(tick_size),
        "fallback_ask": str(fallback_ask),
    }


def fill_notional_usdc(fill: Fill) -> Decimal:
    """Fill の約定名義金額（USDC）を返す。notional_usdc があればそれを使い、なければ price×size。"""
    if fill.notional_usdc is not None:
        try:
            return Decimal(str(fill.notional_usdc))
        except Exception:
            return Decimal("0")
    if fill.price is None or fill.size is None:
        return Decimal("0")
    try:
        return fill.price * fill.size
    except Exception:
        return Decimal("0")


def _metadata_decimal(context: ExtensionContext, *keys: str) -> Decimal | None:
    """按优先顺序从 metadata 中读取十进制数值。"""

    for key in keys:
        value = context.metadata.get(key)
        if value is None:
            continue
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            return None
    return None


def _metadata_text(context: ExtensionContext, *keys: str) -> str | None:
    """按优先顺序从 metadata 中读取文本值。"""

    for key in keys:
        value = context.metadata.get(key)
        if value is not None:
            return str(value)
    return None
