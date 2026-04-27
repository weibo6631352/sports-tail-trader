from __future__ import annotations

from tests.app import admin_api_scenarios as scenarios


def test_create_app_registers_expected_routes() -> None:
    scenarios.run_create_app_registers_expected_routes()


def test_admin_api_exposes_openapi_and_docs_routes() -> None:
    scenarios.run_admin_api_exposes_openapi_and_docs_routes()
