"""Outright 反向定价：以赛季隐含概率为锚，按 edge 退让得到 entry/exit 价位。

纯函数：输入 SeasonOddsSnapshot + 配置，输出 fair_value / entry_cap / exit_target。
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.sports_season import SeasonOddsSnapshot

_PRICE_FLOOR = Decimal("0.01")
_PRICE_CEILING = Decimal("0.99")


def outright_fair_value(snapshot: SeasonOddsSnapshot, outcome_label: str) -> Decimal | None:
    """按 outcome 名找 fair probability，归一到合法概率域。"""

    if snapshot is None:
        return None
    value = snapshot.probability_for(outcome_label)
    if value is None:
        return None
    if value <= 0:
        return None
    return _clamp(value)


def outright_entry_price_cap(
    fair_value: Decimal,
    *,
    min_edge_bps: int,
    max_entry_price: Decimal,
) -> Decimal:
    """入场价上限：fair_value × (1 - edge_required)，并不超过策略硬上限。

    Edge 表示我们要求的最低折扣。``min_edge_bps=500`` 即至少 5% edge：
    fair=0.40 → cap=0.38。
    """

    edge = Decimal(min_edge_bps) / Decimal(10000)
    proportional_cap = fair_value * (Decimal(1) - edge)
    return _clamp(min(proportional_cap, max_entry_price))


def outright_exit_price_target(
    fair_value: Decimal,
    entry_price: Decimal,
    *,
    exit_edge_target: Decimal,
    min_profit_per_share: Decimal,
) -> Decimal:
    """退出目标价：fair_value 上方留 buffer 平仓，或至少高于入场价 + 最小利润。"""

    if min_profit_per_share <= 0:
        raise ValueError(f"min_profit_per_share must be positive, got {min_profit_per_share}")
    target_by_edge = fair_value + exit_edge_target
    target_by_floor = entry_price + min_profit_per_share
    result = _clamp(max(target_by_edge, target_by_floor))
    if result <= entry_price:
        raise ValueError(
            f"exit_price_target={result} <= entry_price={entry_price}; "
            f"fair_value={fair_value} exit_edge={exit_edge_target} min_profit={min_profit_per_share}"
        )
    return result


def _clamp(value: Decimal) -> Decimal:
    if value < _PRICE_FLOOR:
        return _PRICE_FLOOR
    if value > _PRICE_CEILING:
        return _PRICE_CEILING
    return value


__all__ = [
    "outright_fair_value",
    "outright_entry_price_cap",
    "outright_exit_price_target",
]
