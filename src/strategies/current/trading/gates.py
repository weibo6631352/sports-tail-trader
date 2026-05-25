"""Allocation 层共享工具：ask depth / open order / pre-orderbook stub。

历史上承载 _tail_entry_gate / _scale_in_entry_gate / Goalserve cross-validation
等扫尾入场判断，全部删除——「门禁过了即进场」哲学下，allocation 层只保留硬约束
（live source / state / best_ask），进场后行为走 position_plan。
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.order import OrderSide
from polymarket_trader.extension_api import ExtensionContext

from strategies.current.allocation import AllocationMarketSnapshot
from strategies.current.config import CurrentStrategyConfig


def _tail_pre_orderbook_skip_reason(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    snapshot: AllocationMarketSnapshot,
) -> str:
    """保留 stub——allocation.py 仍调用此点。

    历史用途（family unsupported / live state missing）已上移到
    ``_allocation_skip_reason`` 自身的直播源审查；此函数无业务逻辑。
    """

    return ""


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
