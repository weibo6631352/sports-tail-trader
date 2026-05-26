"""Allocation 层共享工具：ask depth / open order 计算。

「门禁过了即进场」量化哲学下，allocation 层只保留硬约束（live source / state /
best_ask），进场后行为走 position_plan + 动态退出引擎。
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.order import OrderSide

from polymarket_trader.workflow.allocation import AllocationMarketSnapshot


def _has_open_order(snapshot: AllocationMarketSnapshot, side: OrderSide) -> bool:
    """判断当前 token 是否已有同方向开放订单，避免入场路径重复占仓。"""

    return any(order.side == side and order.open for order in snapshot.open_orders)


def _ask_depth_notional(orderbook, *, price_cap: Decimal | None = None) -> Decimal:
    """计算价格上限内的 ask 侧深度总额（USDC）。"""

    if orderbook is None:
        return Decimal("0")
    depth_usdc = Decimal("0")
    levels = orderbook.asks
    if not levels and orderbook.best_ask is not None and orderbook.best_ask_size is not None:
        if price_cap is None or orderbook.best_ask <= price_cap:
            return orderbook.best_ask * orderbook.best_ask_size
        return Decimal("0")
    for level in levels:
        if price_cap is None or level.price <= price_cap:
            depth_usdc += level.price * level.size
    return depth_usdc
