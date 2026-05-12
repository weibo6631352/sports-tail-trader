from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from polymarket_trader.app.admin_runtime_view import AdminRuntimeView
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.runtime.registry import MarketRegistry


def _market(index: int) -> Market:
    return Market(
        condition_id=f"condition-{index}",
        market_slug=f"market-{index}",
        outcomes=(MarketOutcome(token_id=f"token-{index}", outcome="YES"),),
    )


class _Readiness:
    def as_dict(self) -> dict[str, object]:
        return {
            "ready_to_trade": True,
            "blocking_issues": [],
            "warnings": [],
        }


class _AccountStore:
    def __init__(self, snapshot: AccountSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> AccountSnapshot:
        return self._snapshot


def test_runtime_snapshot_uses_sample_field_names_for_markets() -> None:
    async def run() -> dict[str, object]:
        registry = MarketRegistry()
        for index in range(25):
            registry.upsert(_market(index))
        runtime = SimpleNamespace(
            registry=registry,
            supervisor=None,
            settings=SimpleNamespace(),
            account_state_store=None,
            event_bus=None,
            persistence_worker=None,
            market_discovery_scan=None,
            sports_live_state_worker=None,
        )
        return await AdminRuntimeView(runtime=runtime).runtime_snapshot()

    payload = asyncio.run(run())

    assert "markets" not in payload
    assert "market_sample" in payload
    registry_payload = payload["registry"]
    assert isinstance(registry_payload, dict)
    assert "markets" not in registry_payload
    assert len(registry_payload["market_sample"]) == 20
    assert registry_payload["market_count"] == 25
    assert registry_payload["markets_truncated"] is True


def test_readiness_warns_when_available_balance_cannot_cover_configured_order_size() -> None:
    account = AccountSnapshot(
        balance_usdc=Decimal("0.405338"),
        allowance_usdc=Decimal("1000"),
        user_ws_connected=True,
        allow_new_entries=True,
        last_reconcile_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
    )
    runtime = SimpleNamespace(
        supervisor=None,
        readiness=_Readiness(),
        settings=SimpleNamespace(
            portfolio_budget_usdc=Decimal("1000000"),
            kelly_max_position_fraction=Decimal("0.10"),
            kelly_min_stake_usdc=Decimal("5"),
        ),
        account_state_store=_AccountStore(account),
        event_bus=None,
        persistence_worker=None,
    )

    payload = AdminRuntimeView(runtime=runtime).readiness_snapshot()

    assert payload["ready_to_trade"] is True
    assert "available_usdc_below_configured_order_size" in payload["warnings"]
    assert "available_usdc_below_configured_order_size" in payload["runtime"]["warnings"]

    runtime_payload = asyncio.run(AdminRuntimeView(runtime=runtime).runtime_snapshot())
    assert "available_usdc_below_configured_order_size" in runtime_payload["readiness"]["warnings"]
    assert "available_usdc_below_configured_order_size" in runtime_payload["runtime"]["warnings"]
