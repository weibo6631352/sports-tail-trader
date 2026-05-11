from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.account_state import AccountStateStore


def _build_position(
    *,
    shares: Decimal,
    open_buy_shares: Decimal = Decimal("0"),
    open_sell_shares: Decimal = Decimal("0"),
    confirmed_shares: Decimal = Decimal("0"),
    token_id: str = "0xtoken",
) -> Position:
    return Position(
        condition_id="0xcondition",
        token_id=token_id,
        shares=shares,
        cost_usdc=Decimal("10"),
        open_buy_shares=open_buy_shares,
        open_sell_shares=open_sell_shares,
        confirmed_shares=confirmed_shares,
    )


def _run_list_positions(positions: tuple[Position, ...]) -> dict[str, object]:
    async def run() -> dict[str, object]:
        account_state = AccountStateStore()
        account_state.mark_user_ws_connected(True)
        account_state.mark_reconciled(datetime(2026, 4, 28, tzinfo=timezone.utc))
        account_state.replace_positions(positions)
        service = AdminService(
            runtime=SimpleNamespace(
                account_state_store=account_state,
                db_session_factory=None,
            )
        )
        return await service.list_positions()

    return asyncio.run(run())


def test_lifecycle_stage_holding_present_on_positions_payload() -> None:
    payload = _run_list_positions(
        (
            _build_position(
                shares=Decimal("100"),
                confirmed_shares=Decimal("100"),
                token_id="0xholding",
            ),
        )
    )

    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["lifecycle_stage"] == "holding"


def test_lifecycle_stage_covers_all_known_branches() -> None:
    payload = _run_list_positions(
        (
            _build_position(
                shares=Decimal("0"),
                open_buy_shares=Decimal("50"),
                token_id="0xentry",
            ),
            _build_position(
                shares=Decimal("100"),
                confirmed_shares=Decimal("100"),
                token_id="0xhold",
            ),
            _build_position(
                shares=Decimal("100"),
                open_sell_shares=Decimal("40"),
                confirmed_shares=Decimal("100"),
                token_id="0xexit",
            ),
            _build_position(
                shares=Decimal("0"),
                confirmed_shares=Decimal("100"),
                token_id="0xclosed",
            ),
        )
    )

    stage_by_token = {
        item["token_id"]: item["lifecycle_stage"] for item in payload["items"]
    }
    assert stage_by_token == {
        "0xentry": "entry_pending",
        "0xhold": "holding",
        "0xexit": "exiting",
        "0xclosed": "closed",
    }
