"""SCOPE 域类型必须强制要求 strategy_id；缺失直接抛 TypeError/ValueError。

CLAUDE.md §10 命名单义、§8 不为兼容旧行为留默认：strategy_id 是框架/策略边界契约的强字段，
任何构造缺失都不能默默写空，必须在构造期就拒绝。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent, Fill
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    Order,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderType,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.domain.position import Position


def test_order_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        Order(  # type: ignore[call-arg]
            condition_id="c",
            token_id="t",
            side=OrderSide.BUY,
            order_type=OrderType.FAK,
            price=Decimal("0.5"),
        )


def test_fill_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        Fill(trace_id="trace")  # type: ignore[call-arg]


def test_position_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        Position(  # type: ignore[call-arg]
            condition_id="c",
            token_id="t",
            shares=Decimal("0"),
            cost_usdc=Decimal("0"),
        )


def test_allocation_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        Allocation(  # type: ignore[call-arg]
            condition_id="c",
            target_budget_usdc=Decimal("0"),
            buy_budget_usdc=Decimal("0"),
        )


def test_audit_event_requires_strategy_id() -> None:
    with pytest.raises(ValueError, match="strategy_id"):
        AuditEvent(event_title="x", trace_id="t")  # type: ignore[call-arg]


def test_decision_record_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        DecisionRecord(  # type: ignore[call-arg]
            trace_id="trace",
            condition_id="cond",
            decision_input={},
            decision_output={},
            accepted=False,
        )


def test_order_result_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        OrderResult(  # type: ignore[call-arg]
            trace_id="trace",
            condition_id="cond",
            token_id="t",
            status=OrderResultStatus.NO_FILL,
        )


def test_buy_intent_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        BuyOrderIntent(  # type: ignore[call-arg]
            trace_id="trace",
            condition_id="cond",
            token_id="t",
            price=Decimal("0.5"),
            amount_usdc=Decimal("1"),
        )


def test_sell_intent_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        SellOrderIntent(  # type: ignore[call-arg]
            trace_id="trace",
            condition_id="cond",
            token_id="t",
            price=Decimal("0.5"),
            size_shares=Decimal("1"),
        )


def test_cancel_intent_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        CancelOrderIntent(  # type: ignore[call-arg]
            trace_id="trace",
            condition_id="cond",
            token_id="t",
            order_id="o",
        )


def test_replace_intent_requires_strategy_id() -> None:
    with pytest.raises(TypeError):
        ReplaceOrderIntent(  # type: ignore[call-arg]
            trace_id="trace",
            condition_id="cond",
            token_id="t",
            order_id="o",
            new_price=Decimal("0.5"),
            size_shares=Decimal("1"),
        )


def test_audit_event_empty_strategy_id_rejected() -> None:
    with pytest.raises(ValueError, match="strategy_id"):
        AuditEvent(event_title="x", trace_id="t", strategy_id="")


def test_audit_event_accepts_strategy_id_via_kwarg() -> None:
    event = AuditEvent(event_title="x", trace_id="t", strategy_id="sports_tail")
    assert event.strategy_id == "sports_tail"


def test_audit_event_accepts_strategy_id_via_payload_only() -> None:
    event = AuditEvent(event_title="x", trace_id="t", payload={"strategy_id": "sports_tail"})
    assert event.strategy_id == "sports_tail"
