"""策略内的 ParameterPort 解析器。

策略关键阈值（``min_edge_bps`` / ``kelly_fraction_cap`` / ``min_profit_per_share``
等）默认从 frozen ``CurrentStrategyConfig`` 读；如果框架注入了 ``ParameterPort``
且该参数有 active override，本模块返回 override 值。

为什么不在 ``CurrentStrategyConfig`` 内部解析：``CurrentStrategyConfig`` 是
frozen dataclass，启动期校验后不应再被替换。Override 是 runtime 临时探索值，
不能跨重启存活——单独走 port 让职责清晰。

两种解析入口：
- ``effective_*(ports, key, default)``：显式传 ports；调用方持有引用时用。
- ``effective_*(None, key, default)``：用 ``ContextVar`` 取 strategy 在 hook
  入口注册的"当前激活 ports"。这条路径让 ``_tail_price_cap`` 等深层 helper
  不必把 ports 一路 plumb 到所有调用点。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

from polymarket_trader.contracts import RuntimePorts

logger = logging.getLogger(__name__)


_active_ports: ContextVar[RuntimePorts | None] = ContextVar(
    "polymarket_trader.quant.active_ports", default=None
)


@contextmanager
def active_ports_scope(ports: RuntimePorts | None) -> Iterator[None]:
    """在 strategy hook 入口处包一层；嵌套调用安全（ContextVar.reset）。"""

    token = _active_ports.set(ports)
    try:
        yield
    finally:
        _active_ports.reset(token)


def _resolve(ports: RuntimePorts | None, key: str, default: Any) -> Any:
    effective_ports = ports if ports is not None else _active_ports.get()
    if effective_ports is None:
        return default
    port = effective_ports.parameter
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
