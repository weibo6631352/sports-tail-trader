"""``DecisionRecord`` 域内 DTO 基础不变量校验。"""

from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.decisions import DecisionRecord


def test_decision_record_normalizes_created_at_to_utc() -> None:
    naive = datetime(2026, 5, 11, 12, 0, 0)
    record = DecisionRecord(
        strategy_id="sports_tail",
        trace_id="trace-x",
        condition_id="cond-x",
        decision_input={"market": "m1"},
        decision_output={"action": "skip"},
        accepted=False,
        created_at=naive,
    )
    assert record.created_at.tzinfo is timezone.utc


def test_decision_record_assigns_record_id_default() -> None:
    record = DecisionRecord(
        strategy_id="sports_tail",
        trace_id="trace-y",
        condition_id="cond-y",
        decision_input={},
        decision_output={},
        accepted=True,
    )
    assert record.record_id  # uuid4 hex
    assert len(record.record_id) == 32


def test_decision_record_preserves_explicit_record_id_and_reason() -> None:
    record = DecisionRecord(
        strategy_id="sports_tail",
        record_id="rid-1",
        trace_id="trace-z",
        condition_id="cond-z",
        decision_input={"x": 1},
        decision_output={"action": "skip", "reason": "no_signal"},
        accepted=False,
        reason="no_signal",
    )
    assert record.record_id == "rid-1"
    assert record.reason == "no_signal"
    assert record.accepted is False
