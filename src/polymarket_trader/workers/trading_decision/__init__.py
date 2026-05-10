"""交易决策 worker 子包。

包含 trading_decision_worker 主体、事件 payload 序列化、订单结果处理与
work-result DTO。模块间通过相对 import 互相依赖；调用方按
``from polymarket_trader.workers.trading_decision import X`` 使用。
"""

from __future__ import annotations

from .event_payloads import (
    TRADING_DECISION_WORKER_ORIGIN,
    coerce_order_result_from_event,
    coerce_order_type,
    coerce_side,
    coerce_status,
    decimal_or_none,
    is_self_emitted,
    market_from_result,
    result_event_type,
    serialize_allocation,
    serialize_allocation_plan,
    serialize_control_intent,
    serialize_intent,
    serialize_order_result,
    serialize_plan_metadata,
    serialize_review,
    serialize_snapshot,
    snapshot_allowance,
    snapshot_available_usdc,
    snapshot_balance,
    snapshot_position,
)
from .order_result_processor import TradingOrderResultProcessor
from .result import TradingDecisionWorkerResult
from .worker import TradingDecisionWorker

__all__ = [
    "TRADING_DECISION_WORKER_ORIGIN",
    "TradingDecisionWorker",
    "TradingDecisionWorkerResult",
    "TradingOrderResultProcessor",
    "coerce_order_result_from_event",
    "coerce_order_type",
    "coerce_side",
    "coerce_status",
    "decimal_or_none",
    "is_self_emitted",
    "market_from_result",
    "result_event_type",
    "serialize_allocation",
    "serialize_allocation_plan",
    "serialize_control_intent",
    "serialize_intent",
    "serialize_order_result",
    "serialize_plan_metadata",
    "serialize_review",
    "serialize_snapshot",
    "snapshot_allowance",
    "snapshot_available_usdc",
    "snapshot_balance",
    "snapshot_position",
]
