"""验证 strategy_id 从 ExtensionContext 一路流到 DecisionRecord / 域事件。

CLAUDE.md §10 命名单义、§8 设计原则：策略身份在 framework / 策略边界一次定型，
通过 ``ExtensionContext.strategy_id`` 流入下游所有记录，不允许在 framework 默认填补。
"""

from __future__ import annotations

from polymarket_trader.app.decision_recorder import (
    DecisionEventRecorder,
    build_decision_record_from_hook,
)
from polymarket_trader.domain.events import OutboxEvent


class _RecordingOutbox:
    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    def put_nowait(self, event: OutboxEvent) -> bool:
        self.events.append(event)
        return True


def test_decision_record_carries_strategy_id_from_hook_call() -> None:
    record = build_decision_record_from_hook(
        hook_name="decide_entry",
        trace_id="trace-x",
        strategy_id="sports_tail",
        context={"hello": "world"},
        decision={"action": "skip", "reason": "no_signal"},
        condition_id="cond-x",
        token_id="tok-x",
        market_slug="slug-x",
    )
    assert record is not None
    assert record.strategy_id == "sports_tail"


def test_decision_record_outbox_payload_includes_strategy_id() -> None:
    outbox = _RecordingOutbox()
    recorder = DecisionEventRecorder(outbox=outbox, strategy_id="sports_tail")
    record = build_decision_record_from_hook(
        hook_name="decide_entry",
        trace_id="trace-x",
        strategy_id="sports_tail",
        context={},
        decision={"action": "buy", "reason": "ok"},
        condition_id="cond-x",
    )
    assert record is not None
    recorder.record(record)
    assert len(outbox.events) == 1
    assert outbox.events[0].payload["strategy_id"] == "sports_tail"


def test_build_decision_record_drops_empty_strategy_id() -> None:
    record = build_decision_record_from_hook(
        hook_name="decide_entry",
        trace_id="trace-x",
        strategy_id="",
        context={},
        decision={"action": "skip"},
        condition_id="cond-x",
    )
    assert record is None


def test_decision_event_recorder_rejects_empty_strategy_id_at_construction() -> None:
    import pytest

    outbox = _RecordingOutbox()
    with pytest.raises(ValueError, match="strategy_id"):
        DecisionEventRecorder(outbox=outbox, strategy_id="")
