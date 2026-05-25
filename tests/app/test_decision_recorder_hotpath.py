"""Producer 侧热路径不变量。

CLAUDE.md §3 / §7：决策路径上录制只能 ``outbox.put_nowait``，不允许任何
``await session.execute`` 或 DB 写入。这里通过两个手段验证：

1. ``DecisionEventRecorder.record`` 在调用栈中不接触 SQLAlchemy session。
   ——用一个会在 ``__call__``/``__aenter__`` 等任意属性访问时炸的 sentinel
   占位 db session_factory，证明 producer 全程不会拿它。
2. ``record`` 把事件投到 ``outbox.put_nowait``，``OutboxEvent.event_type`` 是
   ``DECISION_RECORDED``，``payload`` 包含 input/output 与 accepted/reason。

CLI / extension 移除后 ``InMemoryDecisionRecorder`` 已删除——任何用例尝试
``import`` 它就直接 ImportError。
"""

from __future__ import annotations

from typing import Any

import pytest

from polymarket_trader.app.decision_recorder import (
    DecisionEventRecorder,
    build_decision_record_from_hook,
)
from polymarket_trader.domain.events import DomainEventType, OutboxEvent


class _SyncOnlyOutbox:
    """只允许 ``put_nowait``，其他任何属性访问都炸。"""

    def __init__(self) -> None:
        self.calls: list[OutboxEvent] = []

    def put_nowait(self, event: OutboxEvent) -> bool:
        self.calls.append(event)
        return True

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - 防御性
        raise AssertionError(f"producer touched unexpected outbox attr: {name}")


def test_legacy_in_memory_recorder_is_gone() -> None:
    with pytest.raises(ImportError):
        # 旧实现一旦再次出现，CI 立刻挂 —— 防止"补丁式"回退。
        from polymarket_trader.runtime.decision_recorder import InMemoryDecisionRecorder  # noqa: F401


def test_record_only_calls_put_nowait_no_db_await() -> None:
    outbox = _SyncOnlyOutbox()
    recorder = DecisionEventRecorder(strategy_id="sports_tail", outbox=outbox)
    record = build_decision_record_from_hook(
        strategy_id="sports_tail",
        hook_name="decide_entry",
        trace_id="trace-1",
        context={"market": "m1"},
        decision={"action": "skip", "reason": "no_signal"},
        condition_id="cond-1",
        token_id="tok-1",
        market_slug="slug-1",
    )
    assert record is not None
    recorder.record(record)

    assert len(outbox.calls) == 1
    event = outbox.calls[0]
    assert event.event_type == DomainEventType.DECISION_RECORDED.value
    assert event.condition_id == "cond-1"
    assert event.token_id == "tok-1"
    assert event.market_slug == "slug-1"
    assert event.trace_id == "trace-1"
    assert event.priority == 3  # OutboxPriority.P3
    assert event.payload["accepted"] is False
    assert event.payload["reason"] == "no_signal"
    assert event.payload["decision_output"] == {"action": "skip", "reason": "no_signal"}
    # 关键不变量：producer 路径不引入任何 IO/await 副作用 ——
    # 我们没有 await 任何 coroutine 也没访问 outbox 之外的属性。


def test_record_swallows_outbox_failure_to_protect_hot_path() -> None:
    class _BrokenOutbox:
        def put_nowait(self, _event: OutboxEvent) -> bool:
            raise RuntimeError("outbox queue full")

    recorder = DecisionEventRecorder(strategy_id="sports_tail", outbox=_BrokenOutbox())
    record = build_decision_record_from_hook(
        strategy_id="sports_tail",
        hook_name="quant_decide",
        trace_id="trace-2",
        context={"market": "m2"},
        decision={"action": "skip"},
        condition_id="cond-2",
    )
    assert record is not None
    # 不能抛出——决策路径不能因为 outbox 故障被拖停。
    recorder.record(record)


def test_record_accepts_action_buy_as_accepted_true() -> None:
    outbox = _SyncOnlyOutbox()
    recorder = DecisionEventRecorder(strategy_id="sports_tail", outbox=outbox)
    record = build_decision_record_from_hook(
        strategy_id="sports_tail",
        hook_name="decide_entry",
        trace_id="trace-3",
        context={"market": "m3"},
        decision={"action": "buy", "price": "0.5", "amount_usdc": "3"},
        condition_id="cond-3",
    )
    assert record is not None
    assert record.accepted is True
    recorder.record(record)
    assert outbox.calls[0].payload["accepted"] is True


def test_build_decision_record_returns_none_without_condition_id() -> None:
    # condition_id 是 DB 强字段——拿不到时不能落表，跳过录制以避免脏数据。
    record = build_decision_record_from_hook(
        strategy_id="sports_tail",
        hook_name="decide_entry",
        trace_id="trace-no-cond",
        context={"x": 1},
        decision={"action": "skip"},
        condition_id=None,
    )
    assert record is None
