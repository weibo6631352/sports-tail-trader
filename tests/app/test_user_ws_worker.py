from __future__ import annotations

import asyncio
from decimal import Decimal

from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.order import OrderStatus
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.workers.user_ws_worker import UserWsWorker


def test_user_ws_worker_tracks_balance_position_and_connection_state() -> None:
    async def run() -> None:
        store = AccountStateStore()
        worker = UserWsWorker(account_state_store=store)

        balance_result = await worker.process_message(
            {
                "type": "balance",
                "trace_id": "trace-balance",
                "balance_usdc": "100",
                "allowance_usdc": "90",
            }
        )
        assert balance_result.snapshot.balance_usdc == Decimal("100")
        assert balance_result.snapshot.allowance_usdc == Decimal("90")
        assert balance_result.events[0].event_type == DomainEventType.BALANCE_UPDATED

        position_result = await worker.process_message(
            {
                "type": "position",
                "trace_id": "trace-position",
                "position": {
                    "condition_id": "condition",
                    "token_id": "token",
                    "market_slug": "sample-market-a",
                    "shares": "5",
                    "cost_usdc": "3.0",
                },
            }
        )
        position = position_result.snapshot.get_position("condition", "token")
        assert position is not None
        assert position.shares == Decimal("5")
        assert position_result.events[0].event_type == DomainEventType.POSITION_UPDATED

        disconnected = await worker.set_connection_state(False, trace_id="trace-disconnect")
        assert disconnected.snapshot.allow_new_entries is False

        reconnected = await worker.set_connection_state(True, trace_id="trace-reconnect")
        assert reconnected.snapshot.user_ws_connected is True
        assert reconnected.snapshot.allow_new_entries is False

    asyncio.run(run())


def test_user_ws_worker_requires_reconcile_after_reconnect_before_buying_resumes() -> None:
    async def run() -> None:
        store = AccountStateStore()
        worker = UserWsWorker(account_state_store=store)

        await worker.set_connection_state(True, trace_id="trace-reconnect")
        assert store.snapshot().user_ws_connected is True
        assert store.snapshot().allow_new_entries is False
        assert store.snapshot().last_reconcile_at is None

        store.mark_reconciled()
        assert store.snapshot().last_reconcile_at is not None
        assert store.snapshot().allow_new_entries is True

        await worker.set_connection_state(False, trace_id="trace-disconnect")
        assert store.snapshot().user_ws_connected is False
        assert store.snapshot().allow_new_entries is False
        assert store.snapshot().last_reconcile_at is None

        await worker.set_connection_state(True, trace_id="trace-reconnect-again")
        assert store.snapshot().user_ws_connected is True
        assert store.snapshot().allow_new_entries is False
        assert store.snapshot().last_reconcile_at is None

    asyncio.run(run())


def test_user_ws_worker_opens_entry_gate_when_first_connection_follows_reconcile() -> None:
    async def run() -> None:
        store = AccountStateStore()
        worker = UserWsWorker(account_state_store=store)

        store.mark_reconciled()
        assert store.snapshot().user_ws_connected is False
        assert store.snapshot().allow_new_entries is False

        reconnected = await worker.set_connection_state(True, trace_id="trace-connect-after-reconcile")

        assert reconnected.snapshot.user_ws_connected is True
        assert reconnected.snapshot.last_reconcile_at is not None
        assert reconnected.snapshot.allow_new_entries is True

    asyncio.run(run())


def test_user_ws_worker_builds_official_subscription_payload_and_processes_official_messages() -> None:
    async def run() -> None:
        store = AccountStateStore()
        worker = UserWsWorker(account_state_store=store)

        payload = worker.build_subscription_request(
            ("condition",),
            auth={
                "apiKey": "key",
                "secret": "secret",
                "passphrase": "passphrase",
            },
        )
        assert payload == {
            "auth": {
                "apiKey": "key",
                "secret": "secret",
                "passphrase": "passphrase",
            },
            "markets": ["condition"],
            "type": "user",
        }

        order_result = await worker.process_message(
            {
                "event_type": "order",
                "type": "PLACEMENT",
                "id": "order-1",
                "market": "condition",
                "asset_id": "token",
                "side": "SELL",
                "price": "0.57",
                "original_size": "10",
                "size_matched": "4",
                "timestamp": "1672290687",
            }
        )
        order = order_result.snapshot.open_orders_for_market("condition", "token")[0]
        assert order.order_id == "order-1"
        assert order.size_shares == Decimal("10")
        assert order.filled_shares == Decimal("4")
        assert order.remaining_shares == Decimal("6")
        assert order.status == OrderStatus.PARTIALLY_FILLED

        fill_result = await worker.process_message(
            {
                "event_type": "trade",
                "type": "TRADE",
                "id": "trade-1",
                "market": "condition",
                "asset_id": "token",
                "taker_order_id": "order-1",
                "side": "BUY",
                "price": "0.57",
                "size": "3",
                "status": "MATCHED",
                "matchtime": "1672290701",
                "timestamp": "1672290701",
            }
        )
        position = fill_result.snapshot.get_position("condition", "token")
        assert position is not None
        assert position.shares == Decimal("3")
        assert fill_result.events[0].event_type == DomainEventType.ORDER_STATE_UPDATED
        assert fill_result.events[1].event_type == DomainEventType.FILL_RECORDED

    asyncio.run(run())
