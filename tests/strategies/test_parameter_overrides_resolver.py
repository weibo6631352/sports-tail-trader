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


def test_active_ports_scope_threads_override_to_helpers() -> None:
    """``active_ports_scope`` 让 ``_tail_price_cap`` 这种深层 helper 不传 ports 也能
    读到当前激活的 override——验证 ContextVar 路径正确。"""

    from strategies.current.parameter_overrides import active_ports_scope

    port = _StubParameterPort({("strategy", "entry_no_price_max"): "0.7"})
    ports = ExtensionPorts(parameter=port)
    # 默认 default 是 0.99；scope 内应返回 0.7
    with active_ports_scope(ports):
        assert effective_decimal(None, "entry_no_price_max", Decimal("0.99")) == Decimal("0.7")
    # scope 退出后回落到 default
    assert effective_decimal(None, "entry_no_price_max", Decimal("0.99")) == Decimal("0.99")


def test_active_ports_scope_nested_resets_correctly() -> None:
    """嵌套 scope 用 ContextVar.reset 还原——内层退出不影响外层。"""

    from strategies.current.parameter_overrides import active_ports_scope

    outer_port = _StubParameterPort({("strategy", "entry_no_price_max"): "0.7"})
    inner_port = _StubParameterPort({("strategy", "entry_no_price_max"): "0.5"})
    with active_ports_scope(ExtensionPorts(parameter=outer_port)):
        assert effective_decimal(None, "entry_no_price_max", Decimal("0.99")) == Decimal("0.7")
        with active_ports_scope(ExtensionPorts(parameter=inner_port)):
            assert effective_decimal(None, "entry_no_price_max", Decimal("0.99")) == Decimal("0.5")
        # 内层退出后应回到外层 override
        assert effective_decimal(None, "entry_no_price_max", Decimal("0.99")) == Decimal("0.7")
