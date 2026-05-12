from __future__ import annotations

import logging
from typing import Any

import pytest

from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.infra.outbox import event_sink as event_sink_module
from polymarket_trader.infra.outbox.event_sink import build_domain_event_outbox_sink


class _CollectingOutbox:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def put_nowait(self, event: Any) -> bool:
        self.events.append(event)
        return True


@pytest.mark.parametrize(
    "event_type",
    (
        DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
        DomainEventType.SKIPPED,
    ),
)
def test_non_persistable_event_types_are_dropped(event_type: DomainEventType) -> None:
    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id=f"trace-{event_type.value}",
        event_type=event_type,
        event_id="event-1",
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        payload={"unused": "should-not-enter-outbox"},
    )

    sink(3, event)

    assert outbox.events == []


def test_market_discovered_event_is_persisted_with_stripped_payload() -> None:
    """discovery 事件需要落库（CLAUDE.md §10 可审计），但 raw_market 这类
    重型字段必须在 _project_payload 阶段剔除，避免 audit 表暴涨。"""

    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id="trace-market-discovered",
        event_type=DomainEventType.MARKET_DISCOVERED,
        event_id="event-md-1",
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        reason="market_selected",
        payload={
            "source": "gamma.events_keyset",
            "summary": "nba-game-moneyline",
            "parse_status": "accepted",
            "accepted": True,
            "discovery_kind": "market_discovered",
            "extension_reason": "market_selected",
            "raw_market": {
                "condition_id": "condition-1",
                "large_blob": "x" * 20_000,
            },
            "market": {
                "condition_id": "condition-1",
                "market_slug": "nba-game-moneyline",
                "token_ids": ["token-yes", "token-no"],
            },
        },
    )

    sink(3, event)

    assert len(outbox.events) == 1
    persisted = outbox.events[0]
    assert persisted.event_type == "market_discovered"
    assert persisted.reason == "market_selected"
    assert persisted.payload.get("market", {}).get("market_slug") == "nba-game-moneyline"
    assert "raw_market" not in persisted.payload
    assert persisted.payload.get("accepted") is True
    assert persisted.payload.get("discovery_kind") == "market_discovered"


def test_market_filtered_out_event_is_persisted_for_audit() -> None:
    """universe 拒绝首次目标盘口时必须留下可审计记录。"""

    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id="trace-filtered",
        event_type=DomainEventType.MARKET_FILTERED_OUT,
        event_id="event-mfo-1",
        market_slug="nba-futures-champion",
        condition_id="condition-2",
        reason="market_family_not_single_game",
        payload={
            "source": "gamma.events_keyset",
            "accepted": False,
            "discovery_kind": "market_filtered_out",
            "extension_reason": "market_family_not_single_game",
            "raw_market": {"large_blob": "x" * 20_000},
        },
    )

    sink(3, event)

    assert len(outbox.events) == 1
    persisted = outbox.events[0]
    assert persisted.event_type == "market_filtered_out"
    assert persisted.reason == "market_family_not_single_game"
    assert persisted.payload.get("extension_reason") == "market_family_not_single_game"
    assert "raw_market" not in persisted.payload


def test_trading_paused_event_is_persisted_with_projected_payload() -> None:
    """主交易开关 pause/resume 必须落 outbox→audit_events，否则违反 CLAUDE.md §10。"""

    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id="trace-pause",
        event_type=DomainEventType.TRADING_PAUSED,
        event_id="event-pause-1",
        reason="market_alarm",
        payload={
            "operator": "op",
            "reason": "market_alarm",
            "phase_before": "trading_enabled",
            "phase_after": "paused",
            "occurred_at": "2026-05-11T12:34:56+00:00",
            "extra_unprojected_field": "must-be-dropped",
        },
    )

    sink(1, event)

    assert len(outbox.events) == 1
    persisted = outbox.events[0]
    assert persisted.event_type == DomainEventType.TRADING_PAUSED.value
    assert persisted.reason == "market_alarm"
    assert persisted.payload == {
        "operator": "op",
        "reason": "market_alarm",
        "phase_before": "trading_enabled",
        "phase_after": "paused",
        "occurred_at": "2026-05-11T12:34:56+00:00",
    }


def test_trading_resumed_event_carries_previous_pause_reason() -> None:
    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id="trace-resume",
        event_type=DomainEventType.TRADING_RESUMED,
        event_id="event-resume-1",
        reason="manual_resume",
        payload={
            "operator": "op",
            "phase_before": "paused",
            "phase_after": "trading_enabled",
            "previous_manual_pause_reason": "market_alarm",
            "degraded_reason": None,
            "occurred_at": "2026-05-11T12:35:30+00:00",
        },
    )

    sink(1, event)

    assert len(outbox.events) == 1
    persisted = outbox.events[0]
    assert persisted.event_type == DomainEventType.TRADING_RESUMED.value
    assert persisted.payload["previous_manual_pause_reason"] == "market_alarm"
    assert persisted.payload["phase_after"] == "trading_enabled"


def test_reconcile_events_are_persisted_for_admin_audit() -> None:
    """admin_query.reconcile_decisions / timeline 都查这些 type；
    白名单漏掉会让 /admin/reconcile_diffs 永远返回 0（N16）。"""

    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)

    diff_event = DomainEvent(
        trace_id="trace-diff",
        event_type=DomainEventType.RECONCILE_DIFF_DETECTED,
        event_id="event-diff-1",
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        token_id="token-yes",
        reason="extra_local_open_order",
        payload={
            "action_type": "cancel_external_unknown",
            "source_order_id": "order-xyz",
            "source_order_side": "BUY",
            "target_size_shares": "10",
            "target_notional_usdc": "5",
            "pause_reason": None,
            "metadata": {"trigger": "scheduled"},
            "raw_internal_blob": "should-be-dropped",
        },
    )
    applied_event = DomainEvent(
        trace_id="trace-applied",
        event_type=DomainEventType.RECONCILE_APPLIED,
        event_id="event-applied-1",
        condition_id="condition-1",
        reason="reconcile_applied",
        payload={"action_count": 2, "applied_count": 2, "failed_count": 0},
    )
    started_event = DomainEvent(
        trace_id="trace-started",
        event_type=DomainEventType.RECONCILE_STARTED,
        event_id="event-started-1",
        reason="reconcile_started",
        payload={
            "market_count": 5,
            "paused_market_count": 1,
            "diff_count": 3,
            "trigger_event_type": "reconcile_scheduled",
            "refresh_summary": {"refreshed": 5, "failures": []},
        },
    )
    paused_for_market_event = DomainEvent(
        trace_id="trace-pause-market",
        event_type=DomainEventType.TRADING_PAUSED_FOR_MARKET,
        event_id="event-pause-mkt-1",
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        reason="market_not_tradable",
        payload={"market_status": "closed", "pause_reason": "market_closed"},
    )

    for ev in (diff_event, applied_event, started_event, paused_for_market_event):
        sink(3, ev)

    persisted_types = {persisted.event_type for persisted in outbox.events}
    assert persisted_types == {
        DomainEventType.RECONCILE_DIFF_DETECTED.value,
        DomainEventType.RECONCILE_APPLIED.value,
        DomainEventType.RECONCILE_STARTED.value,
        DomainEventType.TRADING_PAUSED_FOR_MARKET.value,
    }
    by_type = {persisted.event_type: persisted for persisted in outbox.events}
    diff_payload = by_type[DomainEventType.RECONCILE_DIFF_DETECTED.value].payload
    assert diff_payload["action_type"] == "cancel_external_unknown"
    assert "raw_internal_blob" not in diff_payload
    applied_payload = by_type[DomainEventType.RECONCILE_APPLIED.value].payload
    assert applied_payload == {"action_count": 2, "applied_count": 2, "failed_count": 0}
    started_payload = by_type[DomainEventType.RECONCILE_STARTED.value].payload
    assert started_payload["market_count"] == 5
    assert started_payload["refresh_summary"] == {"refreshed": 5, "failures": []}
    paused_payload = by_type[DomainEventType.TRADING_PAUSED_FOR_MARKET.value].payload
    assert paused_payload == {"market_status": "closed", "pause_reason": "market_closed"}


def test_unknown_event_type_warning_is_deduplicated(caplog: pytest.LogCaptureFixture) -> None:
    """同一未知 event_type 出 100 次只 warn 一次——市场 ws 全速跑下避免刷屏（N15）。"""

    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    # caplog 默认抓 propagation；event_sink 的 logger 是 module 名 propagation 链上。
    caplog.set_level(logging.WARNING, logger=event_sink_module.__name__)
    # 隔离测试：清掉 module 级 dedup set 以避免历史轮次干扰
    event_sink_module._unknown_event_types_warned.discard("__test_noise_event__")
    event_sink_module._unknown_event_types_warned.discard("__test_other_noise__")

    for index in range(100):
        sink(
            3,
            DomainEvent(
                trace_id=f"trace-{index}",
                event_type="__test_noise_event__",
                event_id=f"event-{index}",
                payload={},
            ),
        )

    matching = [
        record
        for record in caplog.records
        if "__test_noise_event__" in record.getMessage()
    ]
    assert len(matching) == 1, matching

    # 切换到另一个未知 type 应当再 warn 一次（保证 dedup 不会一刀切吞所有）。
    sink(
        3,
        DomainEvent(
            trace_id="trace-other",
            event_type="__test_other_noise__",
            event_id="event-other-1",
            payload={},
        ),
    )
    matching_other = [
        record
        for record in caplog.records
        if "__test_other_noise__" in record.getMessage()
    ]
    assert len(matching_other) == 1, matching_other


def test_transaction_snapshot_event_is_persisted() -> None:
    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id="trace-order",
        event_type=DomainEventType.ORDER_STATE_UPDATED,
        event_id="event-order-1",
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        token_id="token-yes",
        payload={
            "order": {
                "order_id": "order-1",
                "condition_id": "condition-1",
                "token_id": "token-yes",
                "status": "live",
            },
            "snapshot": {"large_runtime_state": "not-needed"},
        },
    )

    sink(0, event)

    assert len(outbox.events) == 1
    assert outbox.events[0].event_type == DomainEventType.ORDER_STATE_UPDATED.value
    assert outbox.events[0].payload == {
        "order": {
            "order_id": "order-1",
            "condition_id": "condition-1",
            "token_id": "token-yes",
            "status": "live",
        }
    }
