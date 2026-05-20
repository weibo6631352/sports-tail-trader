"""GoalserveClient unit tests: state snapshot, list_events(), health reporting."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from polymarket_trader.domain.sports_live import SportsLiveGameStatus, SportsLiveSourceHealth
from polymarket_trader.infra.sports.goalserve_client import GoalserveClient

_OBSERVED = datetime(2026, 5, 19, tzinfo=timezone.utc)


def _make_client(sports: tuple[str, ...] = ("basketball",)) -> GoalserveClient:
    return GoalserveClient(api_key="test-key", sports=sports)


def _ws_msg(event_id: str, sport: str = "basketball", stp: int = 1) -> dict:
    return {
        "mt": "updt",
        "sp": sport,
        "id": event_id,
        "t1": {"n": "TeamA"},
        "t2": {"n": "TeamB"},
        "stp": stp,
        "et": 1800,
        "sc": "11001",
        "ctry_name": "Test League",
        "st": 1779296400,
        "stats": {"g": [1, 0]},
        "odds": [],
    }


# ---------------------------------------------------------------------------
# list_events from pre-seeded state
# ---------------------------------------------------------------------------

def test_list_events_from_state() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball",))
        # Seed state directly (bypasses WS; simulates received messages)
        async with client._state_lock:
            client._state["basketball"]["ev1"] = _ws_msg("ev1", "basketball")
        client._last_msg_time["basketball"] = time.time()
        snapshot = await client.list_events()
        assert snapshot.source == "goalserve"
        assert len(snapshot.events) == 1
        assert snapshot.events[0].sport == "basketball"
        assert snapshot.events[0].status == SportsLiveGameStatus.LIVE
        await client.aclose()

    asyncio.run(run())


def test_list_events_returns_all_sports() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball", "soccer"))
        async with client._state_lock:
            client._state["basketball"]["ev1"] = _ws_msg("ev1", "basketball")
            client._state["soccer"]["ev2"] = {**_ws_msg("ev2", "soccer"), "stats": {"g": [2, 1], "y": [0, 1], "r": [0, 0], "c": [3, 2]}}
        client._last_msg_time["basketball"] = time.time()
        client._last_msg_time["soccer"] = time.time()
        snapshot = await client.list_events()
        sports = {e.sport for e in snapshot.events}
        assert "basketball" in sports
        assert "soccer" in sports
        await client.aclose()

    asyncio.run(run())


def test_no_sports_returns_empty_snapshot() -> None:
    async def run() -> None:
        client = _make_client(sports=())
        snapshot = await client.list_events()
        return snapshot

    snapshot = asyncio.run(run())
    assert snapshot.events == ()
    assert snapshot.source_statuses == ()


# ---------------------------------------------------------------------------
# Health / source status
# ---------------------------------------------------------------------------

def test_source_status_success_with_data() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball",))
        async with client._state_lock:
            client._state["basketball"]["ev1"] = _ws_msg("ev1")
        client._last_msg_time["basketball"] = time.time()
        snapshot = await client.list_events()
        statuses = {s.source: s for s in snapshot.source_statuses}
        assert "goalserve:basketball" in statuses
        assert statuses["goalserve:basketball"].success is True
        assert statuses["goalserve:basketball"].health == SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
        await client.aclose()

    asyncio.run(run())


def test_source_status_failed_when_too_many_errors() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball",))
        client._consecutive_errors["basketball"] = 4  # > 3 threshold
        snapshot = await client.list_events()
        statuses = {s.source: s for s in snapshot.source_statuses}
        assert statuses["goalserve:basketball"].success is False
        assert statuses["goalserve:basketball"].health == SportsLiveSourceHealth.FAILED
        await client.aclose()

    asyncio.run(run())


def test_source_status_failed_when_stale() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball",))
        # Simulate last message >30s ago
        client._last_msg_time["basketball"] = time.time() - 60
        snapshot = await client.list_events()
        statuses = {s.source: s for s in snapshot.source_statuses}
        assert statuses["goalserve:basketball"].health == SportsLiveSourceHealth.FAILED
        await client.aclose()

    asyncio.run(run())


def test_source_status_empty_when_no_events() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball",))
        snapshot = await client.list_events()
        statuses = {s.source: s for s in snapshot.source_statuses}
        assert statuses["goalserve:basketball"].health == SportsLiveSourceHealth.SUCCESS_EMPTY
        await client.aclose()

    asyncio.run(run())


def test_all_sports_in_source_statuses() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball", "hockey"))
        snapshot = await client.list_events()
        sources = {s.source for s in snapshot.source_statuses}
        assert "goalserve:basketball" in sources
        assert "goalserve:hockey" in sources
        await client.aclose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

def test_handle_message_upserts_event() -> None:
    async def run() -> None:
        client = _make_client(sports=("soccer",))
        msg = _ws_msg("ev1", "soccer")
        await client._handle_message("soccer", msg)
        async with client._state_lock:
            assert "ev1" in client._state["soccer"]
        await client.aclose()

    asyncio.run(run())


def test_handle_message_removes_stp99() -> None:
    async def run() -> None:
        client = _make_client(sports=("soccer",))
        async with client._state_lock:
            client._state["soccer"]["ev1"] = _ws_msg("ev1", "soccer")
        removal_msg = {**_ws_msg("ev1", "soccer"), "stp": 99}
        await client._handle_message("soccer", removal_msg)
        async with client._state_lock:
            assert "ev1" not in client._state["soccer"]
        await client.aclose()

    asyncio.run(run())


def test_handle_message_ignores_unknown_mt() -> None:
    async def run() -> None:
        client = _make_client(sports=("soccer",))
        await client._handle_message("soccer", {"mt": "ping", "id": "ev1"})
        async with client._state_lock:
            assert "ev1" not in client._state["soccer"]
        await client.aclose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# aclose cancels tasks
# ---------------------------------------------------------------------------

def test_aclose_cancels_tasks() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball",))
        # Start tasks
        await client._ensure_started()
        assert len(client._tasks) == 1
        await client.aclose()
        assert all(t.cancelled() or t.done() for t in client._tasks)

    asyncio.run(run())
