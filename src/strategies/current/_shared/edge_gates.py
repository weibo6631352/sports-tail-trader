"""跨 family 共享的入场门禁：edge / 流动性 / 价格上下限。

历史上 outright/evaluator 内联实现了这些校验；series/evaluator 接通 WINNER
后需要同套门禁，所以把"输入 fair_value/best_ask/liquidity/min_edge_bps/
max_entry_price → 通过/拒绝 + reason 字符串"抽到这里。

设计要点：
- 本模块不知道下游枚举（OutrightRejectReason / SeriesRejectReason）。
  ``EdgeCheck.reason`` 是字符串常量（``MISSING_BEST_ASK`` / ``INSUFFICIENT_EDGE`` /
  ``PRICE_ABOVE_FAIR`` / ``LIQUIDITY_BELOW_MIN``），由调用方映射到自家枚举。
- 价格反向定价 ``entry_price_cap = fair × (1 - edge) cap max_entry_price``，
  暴露给调用方做 metadata 透传。
- 全部 Decimal 运算；不接受 None fair_value（应在 pricing 阶段就拒）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

# 价格 floor/ceiling 与 outright/pricing 保持一致——同一份语义只能写一处。
_PRICE_FLOOR = Decimal("0.01")
_PRICE_CEILING = Decimal("0.99")


# 字符串拒绝原因常量。值与对应枚举字面值对齐，让调用方可直接 .value 比较。
REASON_MISSING_BEST_ASK = "missing_best_ask"
REASON_LIQUIDITY_BELOW_MIN = "liquidity_below_min"
REASON_INSUFFICIENT_EDGE = "insufficient_edge"
REASON_PRICE_ABOVE_FAIR = "price_above_fair"


@dataclass(frozen=True, slots=True)
class EdgeCheck:
    """门禁结果。passed=True → reason=None，metadata 含 entry_price_cap 等。

    metadata 字段统一为 ``str(Decimal)``，避免下游 JSON 序列化时丢精度。
    """

    passed: bool
    reason: str | None = None
    entry_price_cap: Decimal | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def entry_price_cap(
    fair_value: Decimal,
    *,
    min_edge_bps: int,
    max_entry_price: Decimal,
) -> Decimal:
    """入场价上限：fair_value × (1 - edge_required)，且不超过策略硬上限。

    与 ``outright/pricing.outright_entry_price_cap`` 数学相同，作为
    family-agnostic 公共实现共享。
    """

    edge = Decimal(min_edge_bps) / Decimal(10000)
    proportional = fair_value * (Decimal(1) - edge)
    return _clamp(min(proportional, max_entry_price))


def check_entry_gates(
    *,
    fair_value: Decimal,
    best_ask: Decimal | None,
    buyable_liquidity_usdc: Decimal,
    min_edge_bps: int,
    max_entry_price: Decimal,
    min_orderbook_depth_usdc: Decimal,
) -> EdgeCheck:
    """一次性跑 best_ask / 流动性 / edge / price-vs-fair 四道门。

    顺序固定：
    1. best_ask 必须非空，否则报 MISSING_BEST_ASK——盘口都不知道何谈成交。
    2. buyable_liquidity_usdc 必须 >= min_orderbook_depth_usdc，否则 LIQUIDITY_BELOW_MIN。
    3. best_ask 必须 <= entry_cap (= fair × (1-edge) ∩ max_entry_price)，
       否则 INSUFFICIENT_EDGE。
    4. best_ask < fair_value（严格）；否则 PRICE_ABOVE_FAIR（无理论收益）。
    """

    if best_ask is None:
        return EdgeCheck(passed=False, reason=REASON_MISSING_BEST_ASK)
    if buyable_liquidity_usdc < min_orderbook_depth_usdc:
        return EdgeCheck(
            passed=False,
            reason=REASON_LIQUIDITY_BELOW_MIN,
            metadata={"buyable_liquidity_usdc": str(buyable_liquidity_usdc)},
        )
    cap = entry_price_cap(
        fair_value,
        min_edge_bps=min_edge_bps,
        max_entry_price=max_entry_price,
    )
    if best_ask > cap:
        return EdgeCheck(
            passed=False,
            reason=REASON_INSUFFICIENT_EDGE,
            entry_price_cap=cap,
            metadata={"best_ask": str(best_ask)},
        )
    if best_ask >= fair_value:
        return EdgeCheck(
            passed=False,
            reason=REASON_PRICE_ABOVE_FAIR,
            entry_price_cap=cap,
            metadata={"best_ask": str(best_ask)},
        )
    return EdgeCheck(
        passed=True,
        entry_price_cap=cap,
        metadata={"best_ask": str(best_ask)},
    )


def _clamp(value: Decimal) -> Decimal:
    if value < _PRICE_FLOOR:
        return _PRICE_FLOOR
    if value > _PRICE_CEILING:
        return _PRICE_CEILING
    return value


__all__ = [
    "EdgeCheck",
    "REASON_INSUFFICIENT_EDGE",
    "REASON_LIQUIDITY_BELOW_MIN",
    "REASON_MISSING_BEST_ASK",
    "REASON_PRICE_ABOVE_FAIR",
    "check_entry_gates",
    "entry_price_cap",
]
