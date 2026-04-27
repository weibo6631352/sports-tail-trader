from __future__ import annotations

from tests.app import admin_api_scenarios as scenarios


def test_admin_api_exposes_hot_state_and_readiness_routes() -> None:
    scenarios.run_admin_api_exposes_hot_state_and_readiness_routes()


def test_admin_ready_route_reports_blockers_when_runtime_is_not_ready() -> None:
    scenarios.run_admin_ready_route_reports_blockers_when_runtime_is_not_ready()


def test_admin_ready_route_exposes_config_blockers_without_runtime_translation() -> None:
    scenarios.run_admin_ready_route_exposes_config_blockers_without_runtime_translation()


def test_admin_ready_route_exposes_runtime_blocking_reasons_after_config_is_ready() -> None:
    scenarios.run_admin_ready_route_exposes_runtime_blocking_reasons_after_config_is_ready()
