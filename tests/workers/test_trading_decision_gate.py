from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from polymarket_trader.app.entry_plan import EntryPlan
from polymarket_trader.domain.allocation import AllocationPlan
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.account import MarketPauseSource
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.workers.trading_decision_worker import TradingDecisionWorker


class _FailingDecisionService:
    def build_entry_plan(self, **_: Any) -> Any:
        raise AssertionError("entry plan should not be built while entry gate is closed")


class _CountingDecisionService:
    def __init__(self) -> None:
        self.calls = 0

    def build_entry_plan(self, **kwargs: Any) -> EntryPlan:
        self.calls += 1
        trace_id = str(kwargs.get("trace_id") or "trace")
        return EntryPlan(
            trace_id=trace_id,
            market=None,
            orderbook=None,
            allocation_plan=AllocationPlan(trace_id=trace_id, total_budget_usdc=Decimal("0")),
            allocation=None,
            intent=None,
            reason="missing_market_state",
        )


def test_orderbook_event_is_skipped_before_sizing_when_account_gate_closed() -> None:
    async def run() -> None:
        worker = TradingDecisionWorker(
            trading_decision_service=_FailingDecisionService(),
            account_state_store=AccountStateStore(),
        )

        result = await worker.process_event(_orderbook_event())

        assert result is None

    asyncio.run(run())


def test_orderbook_event_is_skipped_before_sizing_when_market_is_paused() -> None:
    async def run() -> None:
        account_state = AccountStateStore()
        account_state.mark_user_ws_connected(True)
        account_state.mark_reconciled()
        account_state.pause_market(
            "condition-1",
            reason="manual_pause",
            source=MarketPauseSource.MANUAL,
        )
        worker = TradingDecisionWorker(
            trading_decision_service=_FailingDecisionService(),
            account_state_store=account_state,
        )

        result = await worker.process_event(_orderbook_event())

        assert result is None

    asyncio.run(run())


def test_orderbook_events_are_rate_limited_before_sizing() -> None:
    async def run() -> None:
        account_state = _open_entry_gate()
        decision_service = _CountingDecisionService()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            account_state_store=account_state,
        )

        await worker.process_event(_orderbook_event(event_id="event-orderbook-1"))
        await worker.process_event(_orderbook_event(event_id="event-orderbook-2"))

        assert decision_service.calls == 1

    asyncio.run(run())


def test_explicit_entry_signals_bypass_orderbook_rate_limit() -> None:
    async def run() -> None:
        account_state = _open_entry_gate()
        decision_service = _CountingDecisionService()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            account_state_store=account_state,
        )

        await worker.process_event(_entry_signal_event(event_id="event-entry-1"))
        await worker.process_event(_entry_signal_event(event_id="event-entry-2"))

        assert decision_service.calls == 2

    asyncio.run(run())


def _orderbook_event(*, event_id: str = "event-orderbook") -> DomainEvent:
    return _event(DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED, event_id=event_id)


def _entry_signal_event(*, event_id: str) -> DomainEvent:
    return _event(DomainEventType.ENTRY_SIGNAL_TRIGGERED, event_id=event_id)


def _event(event_type: DomainEventType, *, event_id: str) -> DomainEvent:
    return DomainEvent(
        trace_id="trace-orderbook",
        event_type=event_type,
        event_id=event_id,
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        token_id="token-1",
        payload={"source": "test"},
    )


def _open_entry_gate() -> AccountStateStore:
    account_state = AccountStateStore()
    account_state.mark_user_ws_connected(True)
    account_state.mark_reconciled()
    return account_state
