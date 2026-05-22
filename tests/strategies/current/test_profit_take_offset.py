"""Profit-take 目标价固定 offset 模式回归测试。

multiplier 模式（×1.6）算出的目标价对中高价入场都收敛到 ~0.99，等于持有到
结算、盘中不成交。固定 offset 让目标价盘中可达（买 0.88 → 卖 0.95），实现
准量化的提前止盈。
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from strategies.current.trading.exit_overlay import _profit_take_target_price


def _ctx() -> SimpleNamespace:
    # orderbook / market 皆 None → tick_size 退回 0.01。
    return SimpleNamespace(orderbook=None, market=None)


def test_offset_mode_reachable_mid_game() -> None:
    assert _profit_take_target_price(
        _ctx(), Decimal("0.88"), offset=Decimal("0.07")
    ) == Decimal("0.95")


def test_offset_mode_high_entry_converges_to_cap() -> None:
    # 0.95 + 0.07 = 1.02 → 收敛到 CLOB 上限，不会超过 1。
    target = _profit_take_target_price(_ctx(), Decimal("0.95"), offset=Decimal("0.07"))
    assert target is not None and target <= Decimal("1")
    assert target == Decimal("0.99")


def test_offset_takes_precedence_over_multiplier() -> None:
    assert _profit_take_target_price(
        _ctx(), Decimal("0.88"), offset=Decimal("0.07"), multiplier=Decimal("1.6")
    ) == Decimal("0.95")


def test_multiplier_mode_still_works_when_no_offset() -> None:
    assert _profit_take_target_price(
        _ctx(), Decimal("0.50"), offset=None, multiplier=Decimal("1.6")
    ) == Decimal("0.80")


def test_default_mode_one_tick_when_no_offset_no_multiplier() -> None:
    target = _profit_take_target_price(_ctx(), Decimal("0.90"))
    assert target == Decimal("0.91")
