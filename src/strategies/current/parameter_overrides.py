"""策略内的 ParameterPort 解析器。

策略关键阈值（``min_edge_bps`` / ``kelly_fraction_cap`` / ``min_profit_per_share``
等）默认从 frozen ``CurrentStrategyConfig`` 读；如果框架注入了 ``ParameterPort``
且该参数有 active override，本模块返回 override 值。

为什么不在 ``CurrentStrategyConfig`` 内部解析：``CurrentStrategyConfig`` 是
frozen dataclass，启动期校验后不应再被替换。Override 是 runtime 临时探索值，
不能跨重启存活——单独走 port 让职责清晰。

调用约定：``effective(port, 'strategy_key', config.fallback_field)``。port 为
None（旧测试 / 兼容路径）时直接返回 fallback。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_trader.extension_api import ExtensionPorts


def _resolve(ports: ExtensionPorts | None, key: str, default: Any) -> Any:
    if ports is None:
        return default
    port = getattr(ports, "parameter", None)
    if port is None:
        return default
    try:
        return port.get("strategy", key, default=default)
    except Exception:
        # ParameterPort 行为异常时降级到静态 config，不阻塞热路径
        return default


def effective_int(ports: ExtensionPorts | None, key: str, default: int) -> int:
    """整数 override（bps 之类）。"""

    value = _resolve(ports, key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def effective_decimal(ports: ExtensionPorts | None, key: str, default: Decimal) -> Decimal:
    """Decimal override（价格、USDC 阈值）。"""

    value = _resolve(ports, key, default)
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default
