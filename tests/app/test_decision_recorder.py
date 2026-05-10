from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.app.replay_harness import ReplayDiffKind, replay_records
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.extension_api import ExtensionContext, ExtensionDecision
from polymarket_trader.extension_api.recorder import DecisionRecord
from polymarket_trader.runtime.decision_recorder import (
    InMemoryDecisionRecorder,
    build_decision_record,
)


class _FakeHooks:
    """A minimal hooks stub: decide_exit returns whatever ``decision`` is set."""

    def __init__(self, decision: ExtensionDecision) -> None:
        self._decision = decision

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return ()

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        return self._decision


def _market() -> Market:
    return Market(
        condition_id="cond-1",
        market_slug="slug-1",
        market_question="q?",
        outcomes=(MarketOutcome(outcome="YES", token_id="tok-1"),),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _context() -> ExtensionContext:
    return ExtensionContext(trace_id="trace-1", market=_market(), token_id="tok-1")


def test_in_memory_recorder_keeps_only_capacity_entries() -> None:
    recorder = InMemoryDecisionRecorder(capacity=3)
    for i in range(5):
        recorder.record(
            build_decision_record(
                hook_name="decide_exit",
                trace_id=f"trace-{i}",
                context={"i": i},
                decision={"action": "skip"},
            )
        )
    snapshot = recorder.snapshot()
    assert len(snapshot) == 3
    assert [r.trace_id for r in snapshot] == ["trace-2", "trace-3", "trace-4"]


def test_trading_decision_service_records_decide_exit() -> None:
    decision = ExtensionDecision.skip(reason="no_signal")
    recorder = InMemoryDecisionRecorder()
    service = TradingDecisionService(extension_hooks=_FakeHooks(decision), decision_recorder=recorder)

    result = service.decide_exit(_context())

    assert result is decision
    snapshot = recorder.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].hook_name == "decide_exit"
    assert snapshot[0].trace_id == "trace-1"
    assert snapshot[0].condition_id == "cond-1"
    assert snapshot[0].decision_payload["reason"] == "no_signal"


def test_replay_harness_classifies_unchanged_and_action_changed() -> None:
    record_unchanged = DecisionRecord(
        hook_name="decide_exit",
        trace_id="t-1",
        recorded_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        context_payload={"market": "m1"},
        decision_payload={"action": "skip", "reason": "no_signal", "price": None, "amount_usdc": None, "size_shares": None},
    )
    record_changed = DecisionRecord(
        hook_name="decide_entry",
        trace_id="t-2",
        recorded_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        context_payload={"market": "m2"},
        decision_payload={"action": "buy", "reason": "ok", "price": "0.5", "amount_usdc": "3", "size_shares": None},
    )

    def replay(record: DecisionRecord) -> dict[str, object]:
        if record.trace_id == "t-1":
            return dict(record.decision_payload)
        # 新代码改成 SKIP
        return {"action": "skip", "reason": "tighter_filter", "price": None, "amount_usdc": None, "size_shares": None}

    report = replay_records([record_unchanged, record_changed], replay_decision=replay)

    assert report.changed_count == 1
    by_kind = report.by_kind
    assert by_kind[ReplayDiffKind.UNCHANGED] == 1
    assert by_kind[ReplayDiffKind.ACTION_CHANGED] == 1


def test_replay_harness_marks_replay_error() -> None:
    record = DecisionRecord(
        hook_name="decide_exit",
        trace_id="t-3",
        recorded_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        context_payload={},
        decision_payload={"action": "skip"},
    )

    def replay(record: DecisionRecord) -> dict[str, object]:
        raise RuntimeError("hook crashed")

    report = replay_records([record], replay_decision=replay)
    assert report.diffs[0].kind == ReplayDiffKind.OTHER
    assert "hook crashed" in report.diffs[0].detail
