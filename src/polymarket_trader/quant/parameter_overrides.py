"""策略内的 ParameterPort 解析器。

策略关键阈值（``min_edge_bps`` / ``kelly_fraction_cap`` / ``min_profit_per_share``
等）默认从 frozen ``CurrentStrategyConfig`` 读；如果框架注入了 ``ParameterPort``
且该参数有 active override，本模块返回 override 值。

为什么不在 ``CurrentStrategyConfig`` 内部解析：``CurrentStrategyConfig`` 是
frozen dataclass，启动期校验后不应再被替换。Override 是 runtime 临时探索值，
不能跨重启存活——单独走 port 让职责清晰。

所有 ``effective_*`` 入口都显式接 ``ports`` 参数（``None`` 表示无 override，
直接返回 default）；调用方负责把 ports 从子策略入口一路传到具体 helper。
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_trader.runtime.runtime_ports import RuntimePorts

logger = logging.getLogger(__name__)


def _resolve(ports: RuntimePorts | None, key: str, default: Any) -> Any:
    if ports is None:
        return default
    port = ports.parameter
    if port is None:
        return default
    try:
        return port.get("strategy", key, default=default)
    except Exception as exc:
        logger.warning("parameter_port_error: key=%s, degrading to static config: %s", key, exc)
        return default


def effective_int(ports: RuntimePorts | None, key: str, default: int) -> int:
    """整数 override（bps 之类）。"""

    value = _resolve(ports, key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def effective_decimal(ports: RuntimePorts | None, key: str, default: Decimal) -> Decimal:
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


def effective_str_enum(
    ports: RuntimePorts | None,
    key: str,
    default: Any,
    enum_type: type,
) -> Any:
    """StrEnum override；value 必须是枚举成员字符串，否则退回 default。"""

    value = _resolve(ports, key, default)
    if value is None:
        return default
    try:
        return enum_type(str(value))
    except ValueError:
        return default
