"""量化决策可直接调用的纯函数工具集。

所有工具都是 framework 中性的、无状态的。Domain 层金额/价格仍按 ``Decimal`` 处理。
"""

from polymarket_trader.workflow.toolkit.decimal_math import (
    clamp,
    pct_of,
    round_to_tick,
)
from polymarket_trader.workflow.toolkit.idempotency import build_entry_key
from polymarket_trader.workflow.toolkit.market_filter import MarketFilterDSL
from polymarket_trader.workflow.toolkit.orderbook_tools import (
    depth_at_price,
    depth_weighted_price,
    midpoint,
    spread_bps,
)
from polymarket_trader.workflow.toolkit.position_projection import (
    avg_cost,
    exposure_usdc,
    unrealized_pnl,
)

__all__ = (
    "MarketFilterDSL",
    "avg_cost",
    "build_entry_key",
    "clamp",
    "depth_at_price",
    "depth_weighted_price",
    "exposure_usdc",
    "midpoint",
    "pct_of",
    "round_to_tick",
    "spread_bps",
    "unrealized_pnl",
)
