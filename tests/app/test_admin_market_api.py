from __future__ import annotations

from tests.app import admin_api_scenarios as scenarios


def test_admin_api_supports_fee_filters_and_sorting() -> None:
    scenarios.run_admin_api_supports_fee_filters_and_sorting()


def test_markets_orderbook_falls_back_to_clob_when_hot_snapshot_missing() -> None:
    scenarios.run_markets_orderbook_falls_back_to_clob_when_hot_snapshot_missing()


def test_markets_midpoint_falls_back_to_clob_when_hot_snapshot_missing() -> None:
    scenarios.run_markets_midpoint_falls_back_to_clob_when_hot_snapshot_missing()
