from __future__ import annotations

import asyncio
from types import SimpleNamespace

from polymarket_trader.app.admin_runtime_view import AdminRuntimeView
from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.runtime.registry import MarketRegistry


def _market(index: int) -> Market:
    return Market(
        condition_id=f"condition-{index}",
        market_slug=f"market-{index}",
        outcomes=(MarketOutcome(token_id=f"token-{index}", outcome="YES"),),
    )


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
