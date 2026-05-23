"""Data-api DTO → Position 转换：mark-to-market 字段不写防止 stale 覆盖。

task #35 根因：Polymarket data-api `/positions` endpoint 返回的 currentValue /
curPrice 是缓存值（last_trade 或 stale settlement），不反映实时 best_bid。
若 reconcile 把这些 stale 值写到 Position，会通过 `_merge_position_authority_fields`
覆盖 worker 实时基于 orderbook 设的 cv=0（best_bid=None 时）。

修复：to_position 一律不写 current_value / cur_price / cash_pnl / percent_pnl，
只 worker `_handle_orderbook_snapshot_updated` 是 MTM 真相源。
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.infra.polymarket.schemas.data import normalize_position_payload


def test_to_position_does_not_write_current_value_from_data_api() -> None:
    """data-api 返回 currentValue=$59 不应写入 Position（避免 stale 覆盖 worker MTM）。"""
    payload = {
        "conditionId": "0xabc",
        "asset": "0xdef",
        "shares": "100",
        "cost_usdc": "50.0",
        "currentValue": "59.0",  # stale data-api 缓存
        "curPrice": "0.99",
        "cashPnl": "9.0",
        "percentPnl": "0.18",
        "realizedPnl": "5.0",  # 已结算 PnL 仍写入（事实，不是 MTM）
    }
    dto = normalize_position_payload(payload)
    position = dto.to_position(strategy_id="test")
    # mark-to-market 字段全部 None
    assert position.current_value is None
    assert position.cur_price is None
    assert position.cash_pnl is None
    assert position.percent_pnl is None
    # 账户事实字段保留
    assert position.shares == Decimal("100")
    assert position.cost_usdc == Decimal("50.0")
    assert position.realized_pnl == Decimal("5.0")


def test_to_position_preserves_account_facts() -> None:
    """account fact 字段（shares / cost / avg_price / initial_value /
    realized_pnl）必须从 data-api 写入——这些是结算事实，不是 MTM。
    """
    payload = {
        "conditionId": "0xabc",
        "asset": "0xdef",
        "shares": "100",
        "cost_usdc": "50.0",
        "avgPrice": "0.50",
        "initialValue": "50.0",
        "realizedPnl": "10.0",
        "percentRealizedPnl": "0.20",
        "redeemable": True,
    }
    dto = normalize_position_payload(payload)
    position = dto.to_position(strategy_id="test")
    assert position.shares == Decimal("100")
    assert position.cost_usdc == Decimal("50.0")
    assert position.avg_price == Decimal("0.50")
    assert position.initial_value == Decimal("50.0")
    assert position.realized_pnl == Decimal("10.0")
    assert position.percent_realized_pnl == Decimal("0.20")
    assert position.redeemable is True
