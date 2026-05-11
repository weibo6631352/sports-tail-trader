"""``strategies.current.parameter_overrides`` 解析器行为。

覆盖：
- ``ports=None`` / ``ports.parameter=None`` 时回退到 default
- 有 ``parameter`` port 时返回 override 值
- coerce 失败时仍回退 default（异常不传播到热路径）
- ``int`` / ``Decimal`` 两种类型的覆盖路径
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.extension_api import ExtensionPorts
from strategies.current.parameter_overrides import effective_decimal, effective_int


class _StubParameterPort:
    def __init__(self, overrides: dict[tuple[str, str], object]) -> None:
        self._overrides = overrides

    def get(self, scope: str, key: str, *, default: object = None) -> object:
        return self._overrides.get((scope, key), default)

    def has_override(self, scope: str, key: str) -> bool:
        return (scope, key) in self._overrides


def test_returns_default_when_ports_is_none() -> None:
    assert effective_int(None, "tail_outright_min_edge_bps", 500) == 500
    assert effective_decimal(None, "tail_outright_min_profit_per_share", Decimal("0.02")) == Decimal("0.02")


def test_returns_default_when_parameter_port_missing() -> None:
    ports = ExtensionPorts()  # parameter=None
    assert effective_int(ports, "tail_outright_min_edge_bps", 500) == 500


def test_returns_override_when_port_has_value() -> None:
    port = _StubParameterPort({("strategy", "tail_outright_min_edge_bps"): 350})
    ports = ExtensionPorts(parameter=port)
    assert effective_int(ports, "tail_outright_min_edge_bps", 500) == 350


def test_decimal_override_coerced_from_string() -> None:
    port = _StubParameterPort({("strategy", "tail_outright_min_profit_per_share"): "0.05"})
    ports = ExtensionPorts(parameter=port)
    result = effective_decimal(ports, "tail_outright_min_profit_per_share", Decimal("0.02"))
    assert result == Decimal("0.05")


def test_int_override_invalid_falls_back_to_default() -> None:
    port = _StubParameterPort({("strategy", "tail_outright_min_edge_bps"): "not-an-int"})
    ports = ExtensionPorts(parameter=port)
    assert effective_int(ports, "tail_outright_min_edge_bps", 500) == 500


def test_decimal_override_invalid_falls_back_to_default() -> None:
    port = _StubParameterPort({("strategy", "tail_outright_min_profit_per_share"): "not-a-decimal"})
    ports = ExtensionPorts(parameter=port)
    assert effective_decimal(ports, "tail_outright_min_profit_per_share", Decimal("0.02")) == Decimal("0.02")


def test_port_raising_exception_falls_back_to_default() -> None:
    class _BrokenPort:
        def get(self, scope: str, key: str, *, default: object = None) -> object:
            raise RuntimeError("boom")

        def has_override(self, scope: str, key: str) -> bool:
            return False

    ports = ExtensionPorts(parameter=_BrokenPort())
    assert effective_int(ports, "tail_outright_min_edge_bps", 500) == 500


def test_none_value_falls_back_to_default() -> None:
    port = _StubParameterPort({("strategy", "tail_outright_min_edge_bps"): None})
    ports = ExtensionPorts(parameter=port)
    assert effective_int(ports, "tail_outright_min_edge_bps", 500) == 500
