"""持仓投影工具：避免重复实现 PnL / 平均成本 / 资金暴露计算。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.position import Position


def avg_cost(position: Position | None) -> Decimal | None:
    """返回平均买入成本（cost_usdc / shares）。无持仓或 shares 为 0 返回 None。"""

    if position is None or position.shares <= 0:
        return None
    return position.cost_usdc / position.shares


def unrealized_pnl(position: Position | None, *, mark_price: Decimal | None) -> Decimal | None:
    """按 mark_price 计算未实现盈亏。任一缺失返回 None。"""

    if position is None or mark_price is None or position.shares <= 0:
        return None
    return position.shares * mark_price - position.cost_usdc


def exposure_usdc(position: Position | None, *, mark_price: Decimal | None) -> Decimal:
    """按 mark_price 折算当前资金暴露（shares * mark_price）；缺数据返回 0。"""

    if position is None or mark_price is None or position.shares <= 0:
        return Decimal("0")
    return position.shares * mark_price
