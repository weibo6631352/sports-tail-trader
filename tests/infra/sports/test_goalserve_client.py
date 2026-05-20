"""GoalserveClient unit tests: HTTP mock, multi-sport concurrent, single-sport timeout."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from polymarket_trader.infra.sports.goalserve_client import GoalserveClient

_OBSERVED = datetime(2026, 5, 19, tzinfo=timezone.utc)
_FIXTURE_DIR = Path("tests/fixtures/sports_live/goalserve")


def _load_fixture_bytes(filename: str) -> bytes:
    return (_FIXTURE_DIR / filename).read_bytes()


def _load_fixture_json(filename: str) -> dict:
    return json.loads((_FIXTURE_DIR / filename).read_text())


# ---------------------------------------------------------------------------
# Helpers to mock _fetch_sport directly (avoids httpx transport wiring)
# ---------------------------------------------------------------------------

def _make_client(sports: tuple[str, ...] = ("basketball",)) -> GoalserveClient:
    with patch("httpx.AsyncClient"):
        return GoalserveClient(sports=sports)


# ---------------------------------------------------------------------------
# Basic pass-through
# ---------------------------------------------------------------------------

def test_list_events_returns_snapshot_with_events() -> None:
    basket_data = _load_fixture_json("basketball_sample.json")

    async def run() -> None:
        client = _make_client(sports=("basketball",))
        with patch.object(client, "_fetch_sport", new_callable=AsyncMock) as mock_fetch:
            raw_count = len(basket_data.get("events", {}))
            from polymarket_trader.infra.sports.goalserve_parsers import parse_goalserve_sport
            events = parse_goalserve_sport("basketball", basket_data, observed_at=_OBSERVED)
            mock_fetch.return_value = (events, raw_count)
            snapshot = await client.list_events()

        assert snapshot.source == "goalserve"
        assert len(snapshot.events) >= 1
        assert all(e.source == "goalserve" for e in snapshot.events)

    asyncio.run(run())


def test_source_status_success_reported() -> None:
    basket_data = _load_fixture_json("basketball_sample.json")

    async def run() -> None:
        client = _make_client(sports=("basketball",))
        with patch.object(client, "_fetch_sport", new_callable=AsyncMock) as mock_fetch:
            from polymarket_trader.infra.sports.goalserve_parsers import parse_goalserve_sport
            events = parse_goalserve_sport("basketball", basket_data, observed_at=_OBSERVED)
            raw_count = len(basket_data.get("events", {}))
            mock_fetch.return_value = (events, raw_count)
            snapshot = await client.list_events()

        statuses = {s.source: s for s in snapshot.source_statuses}
        assert "goalserve:basketball" in statuses
        assert statuses["goalserve:basketball"].success is True
        assert statuses["goalserve:basketball"].events_seen >= 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Multi-sport
# ---------------------------------------------------------------------------

def test_multiple_sports_fetched() -> None:
    basket_data = _load_fixture_json("basketball_sample.json")
    hockey_data = _load_fixture_json("hockey_sample.json")

    async def run() -> None:
        client = _make_client(sports=("basketball", "hockey"))
        with patch.object(client, "_fetch_sport", new_callable=AsyncMock) as mock_fetch:
            from polymarket_trader.infra.sports.goalserve_parsers import parse_goalserve_sport
            basket_events = parse_goalserve_sport("basketball", basket_data, observed_at=_OBSERVED)
            hockey_events = parse_goalserve_sport("hockey", hockey_data, observed_at=_OBSERVED)

            async def side_effect(sport, observed_at):
                if sport == "basketball":
                    return basket_events, len(basket_data.get("events", {}))
                return hockey_events, len(hockey_data.get("events", {}))

            mock_fetch.side_effect = side_effect
            snapshot = await client.list_events()

        statuses = {s.source: s for s in snapshot.source_statuses}
        assert "goalserve:basketball" in statuses
        assert "goalserve:hockey" in statuses

    asyncio.run(run())


def test_single_sport_timeout_does_not_block_others() -> None:
    """basketball 超时 → hockey 仍正常返回，source_statuses 各自独立。"""
    hockey_data = _load_fixture_json("hockey_sample.json")

    async def run() -> None:
        client = _make_client(sports=("basketball", "hockey"))
        with patch.object(client, "_fetch_sport", new_callable=AsyncMock) as mock_fetch:
            from polymarket_trader.infra.sports.goalserve_parsers import parse_goalserve_sport
            hockey_events = parse_goalserve_sport("hockey", hockey_data, observed_at=_OBSERVED)

            async def side_effect(sport, observed_at):
                if sport == "basketball":
                    raise httpx.TimeoutException("timeout", request=MagicMock())
                return hockey_events, len(hockey_data.get("events", {}))

            mock_fetch.side_effect = side_effect
            snapshot = await client.list_events()

        statuses = {s.source: s for s in snapshot.source_statuses}
        assert statuses["goalserve:basketball"].success is False
        assert statuses["goalserve:basketball"].last_error is not None
        assert statuses["goalserve:hockey"].success is True
        hockey_events_out = [e for e in snapshot.events if e.sport == "ice-hockey"]
        assert len(hockey_events_out) >= 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_network_error_recorded_in_source_status() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball",))
        with patch.object(client, "_fetch_sport", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = httpx.ConnectError("refused")
            snapshot = await client.list_events()

        statuses = {s.source: s for s in snapshot.source_statuses}
        assert statuses["goalserve:basketball"].success is False
        assert "refused" in (statuses["goalserve:basketball"].last_error or "")

    asyncio.run(run())


def test_all_sports_failed_returns_empty_events() -> None:
    async def run() -> None:
        client = _make_client(sports=("basketball", "hockey"))
        with patch.object(client, "_fetch_sport", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = RuntimeError("network_down")
            snapshot = await client.list_events()

        assert snapshot.events == ()
        assert all(not s.success for s in snapshot.source_statuses)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# No sports configured
# ---------------------------------------------------------------------------

def test_no_sports_returns_empty_snapshot() -> None:
    async def run() -> None:
        client = _make_client(sports=())
        snapshot = await client.list_events()
        return snapshot

    snapshot = asyncio.run(run())
    assert snapshot.events == ()
    assert snapshot.source_statuses == ()


# ---------------------------------------------------------------------------
# Constructor accepts proxy parameter
# ---------------------------------------------------------------------------

def test_proxy_parameter_accepted_without_error() -> None:
    """proxy URL 参数可以传入，不影响构造。"""
    with patch("httpx.AsyncHTTPTransport"):
        with patch("httpx.AsyncClient"):
            client = GoalserveClient(sports=("basketball",), proxy="http://127.0.0.1:7890")
    assert client is not None
