"""Decimal 计算工具：策略侧避免重复实现 clamp / 取整 / 百分比。"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


def clamp(value: Decimal, *, lower: Decimal | None = None, upper: Decimal | None = None) -> Decimal:
    """把 value 收敛到 [lower, upper] 区间。两端都允许 None。"""

    if lower is not None and value < lower:
        return lower
    if upper is not None and value > upper:
        return upper
    return value


def round_to_tick(price: Decimal, tick_size: Decimal, *, rounding: str = "down") -> Decimal:
    """把价格按 tick_size 取整。

    rounding="down" 对应 BUY 价格（不能高于策略意图）；rounding="half_up" 用于
    展示场景。tick_size 必须 > 0；若为 0 直接返回原值（兼容尚未配置 tick 的市场）。
    """

    if tick_size <= 0:
        return price
    quantized = (price / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN if rounding == "down" else ROUND_HALF_UP)
    return quantized * tick_size


def pct_of(numerator: Decimal, denominator: Decimal) -> Decimal:
    """安全百分比：分母为 0 时返回 0，避免策略侧重复写 if。"""

    if denominator == 0:
        return Decimal("0")
    return (numerator / denominator) * Decimal("100")
