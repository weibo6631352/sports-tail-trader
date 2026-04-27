from __future__ import annotations

from polymarket_trader.runtime import discovery_runner


def test_full_market_discovery_defaults_keep_background_sla() -> None:
    assert discovery_runner._MARKET_DISCOVERY_REQUEST_BUDGET_PER_TICK == 2
    assert discovery_runner._MARKET_DISCOVERY_MAX_RUNTIME_MS == 200.0
    assert discovery_runner.MARKET_DISCOVERY_TICK_SECONDS == 0.5
