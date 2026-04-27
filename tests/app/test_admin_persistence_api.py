from __future__ import annotations

from tests.app import admin_api_scenarios as scenarios


def test_admin_api_exposes_audit_allocations_outbox_and_order_id_filter(monkeypatch) -> None:
    scenarios.run_admin_api_exposes_audit_allocations_outbox_and_order_id_filter(monkeypatch)


def test_admin_api_outbox_pending_prefers_live_runtime_queue() -> None:
    scenarios.run_admin_api_outbox_pending_prefers_live_runtime_queue()
