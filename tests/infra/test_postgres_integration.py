from __future__ import annotations

import os
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.config import load_settings
from polymarket_trader.infra.db import Base, DatabasePersistenceRepository, build_session_factory, initialize_database
from polymarket_trader.main import _check_database_connection, _load_reference_state
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.registry import MarketRegistry

pytestmark = pytest.mark.asyncio


def _integration_database_url() -> str | None:
    if explicit := os.environ.get("TRADER_TEST_POSTGRES_DSN"):
        return explicit
    try:
        settings = load_settings()
    except Exception:
        return None
    if settings.database_password is None or not settings.database_password.get_secret_value().strip():
        return None
    return settings.database_url


async def _truncate_all_tables(session_factory: async_sessionmaker) -> None:
    engine = getattr(session_factory, "kw", {}).get("bind")
    if engine is None:
        return
    table_names = [table.name for table in reversed(Base.metadata.sorted_tables)]
    if not table_names:
        return
    joined = ", ".join(f'"{table_name}"' for table_name in table_names)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def postgres_session_factory() -> async_sessionmaker:
    database_url = _integration_database_url()
    if database_url is None:
        pytest.skip("PostgreSQL integration DSN is not configured")

    await initialize_database(database_url)
    session_factory = build_session_factory(database_url)
    if not await _check_database_connection(session_factory):
        bind = getattr(session_factory, "kw", {}).get("bind")
        if bind is not None:
            await bind.dispose()
        pytest.skip("PostgreSQL integration database is unavailable")

    await _truncate_all_tables(session_factory)
    try:
        yield session_factory
    finally:
        await _truncate_all_tables(session_factory)
        bind = getattr(session_factory, "kw", {}).get("bind")
        if bind is not None:
            await bind.dispose()


async def test_initialize_database_creates_expected_tables(
    postgres_session_factory: async_sessionmaker,
) -> None:
    engine = getattr(postgres_session_factory, "kw", {}).get("bind")
    assert engine is not None

    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        )
        table_names = tuple(rows.scalars().all())

    assert table_names == (
        "account_snapshots",
        "allocations",
        "audit_events",
        "fills",
        "markets",
        "orderbook_snapshots",
        "orders",
        "outbox_events",
        "positions",
    )


async def test_persistence_repository_and_admin_service_round_trip(
    postgres_session_factory: async_sessionmaker,
) -> None:
    repository = DatabasePersistenceRepository(postgres_session_factory)
    await repository.save_market_snapshot(
        {
            "trace_id": "trace-market",
            "source": "integration_test",
            "condition_id": "condition-500m",
            "market_slug": "sample-market-a",
            "no_token_id": "no-token",
            "yes_token_id": "yes-token",
            "event_id": "event-1",
            "event_title": "Sample market A in 2026?",
            "event_slug": "sample-event-group",
            "tick_size": "0.01",
            "min_order_size": "1",
            "neg_risk": False,
            "fees_enabled": True,
            "maker_base_fee_bps": 0,
            "taker_base_fee_bps": 100,
            "fee_rate_bps": 125,
            "fee_rate_updated_at": "2026-01-01T12:02:00+00:00",
            "category": "Crypto",
            "tags": ["sample", "market"],
            "matched_keywords": ["sample", "market", "threshold"],
            "trading_status": "eligible",
        }
    )
    await repository.save_market_snapshot(
        {
            "trace_id": "trace-market-2",
            "source": "integration_test",
            "condition_id": "condition-1b",
            "market_slug": "sample-market-b",
            "no_token_id": "no-token-1b",
            "yes_token_id": "yes-token-1b",
            "event_id": "event-2",
            "event_title": "Sample market B in 2026?",
            "event_slug": "sample-market-b",
            "tick_size": "0.01",
            "min_order_size": "1",
            "neg_risk": False,
            "fees_enabled": True,
            "maker_base_fee_bps": 5,
            "taker_base_fee_bps": 150,
            "fee_rate_bps": 200,
            "fee_rate_updated_at": "2026-01-01T12:03:00+00:00",
            "category": "Crypto",
            "tags": ["sample", "market"],
            "matched_keywords": ["sample", "market", "secondary"],
            "trading_status": "eligible",
        }
    )
    await repository.save_account_snapshot(
        {
            "trace_id": "trace-balance",
            "balance_usdc": "120",
            "allowance_usdc": "90",
            "user_ws_connected": True,
            "allow_new_entries": True,
            "market_pauses": [
                {
                    "condition_id": "condition-500m",
                    "reason": "manual_pause",
                    "source": "manual",
                    "recoverable": False,
                }
            ],
            "last_reconcile_at": "2026-01-01T12:05:00+00:00",
        }
    )
    await repository.save_fill(
        {
            "trace_id": "trace-fill",
            "event_type": "trade_confirmed",
            "event_id": "fill-1",
            "market_slug": "sample-market-a",
            "condition_id": "condition-500m",
            "token_id": "no-token",
            "order_id": "sell-1",
            "trade_id": "trade-1",
            "side": "SELL",
            "price": "0.78",
            "size": "3",
            "notional_usdc": "2.10",
            "status": "confirmed",
        }
    )

    runtime = SimpleNamespace(
        db_session_factory=postgres_session_factory,
        registry=MarketRegistry(),
        account_state_store=AccountStateStore(),
    )
    runtime.market_ws_worker = SimpleNamespace(track_market=runtime.registry.upsert)
    loaded_reference = await _load_reference_state(runtime)
    service = AdminService(runtime=runtime)

    markets = await service.list_markets(limit=10, offset=0)
    filtered_markets = await service.list_markets(
        limit=10,
        offset=0,
        fee_rate_bps_min=150,
        sort_by="fee_rate_bps",
        sort_direction="desc",
    )
    fills = await service.list_fills(limit=10, offset=0)

    assert markets["total"] == 2
    assert {item["market"]["market_slug"] for item in markets["items"]} == {
        "sample-market-a",
        "sample-market-b",
    }
    assert filtered_markets["total"] == 1
    assert filtered_markets["items"][0]["market"]["market_slug"] == "sample-market-b"
    assert filtered_markets["items"][0]["market"]["trading_status"] == "eligible"
    assert filtered_markets["items"][0]["market"]["fees"]["enabled"] is True
    assert filtered_markets["items"][0]["market"]["fees"]["taker_base_fee_bps"] == 150
    assert filtered_markets["items"][0]["market"]["fees"]["fee_rate_bps"] == 200

    assert fills["total"] == 1
    assert fills["items"][0]["trace_id"] == "trace-fill"
    assert fills["items"][0]["trade_id"] == "trade-1"
    assert fills["items"][0]["status"] == "confirmed"
    assert loaded_reference["account_snapshots"] == 1
    assert runtime.account_state_store.snapshot().balance_usdc == Decimal("120")
    assert runtime.account_state_store.snapshot().allowance_usdc == Decimal("90")
    assert runtime.account_state_store.snapshot().user_ws_connected is False
    assert runtime.account_state_store.snapshot().allow_new_entries is False
    assert runtime.account_state_store.snapshot().market_pauses == ()
    assert runtime.account_state_store.snapshot().last_reconcile_at is None
