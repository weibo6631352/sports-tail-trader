from __future__ import annotations

from tests.app import admin_api_scenarios as scenarios


def test_admin_api_supports_reconcile_and_replace_routes() -> None:
    scenarios.run_admin_api_supports_reconcile_and_replace_routes()
