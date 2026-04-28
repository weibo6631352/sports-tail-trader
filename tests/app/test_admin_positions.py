from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.runtime.account_state import AccountStateStore


class _FailingSessionFactory:
    def __call__(self) -> object:
        raise AssertionError("fresh authoritative account state must not fall back to stale DB positions")


def test_fresh_empty_account_positions_do_not_fall_back_to_database() -> None:
    async def run() -> dict[str, object]:
        account_state = AccountStateStore()
        account_state.mark_user_ws_connected(True)
        account_state.mark_reconciled(datetime(2026, 4, 28, tzinfo=timezone.utc))
        service = AdminService(
            runtime=SimpleNamespace(
                account_state_store=account_state,
                db_session_factory=_FailingSessionFactory(),
            )
        )
        return await service.list_positions()

    payload = asyncio.run(run())

    assert payload["total"] == 0
    assert payload["items"] == []
