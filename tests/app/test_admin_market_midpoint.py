from __future__ import annotations

import asyncio
from types import SimpleNamespace

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.infra.polymarket import PolymarketClientError


class _MissingMidpointClient:
    async def get_midpoint(self, _token_id: str):
        raise PolymarketClientError(
            "midpoint not found",
            operation="clob.get_midpoint",
            status_code=404,
        )


def test_missing_midpoint_returns_empty_payload_instead_of_upstream_error() -> None:
    async def run() -> dict[str, object] | None:
        service = AdminService(
            runtime=SimpleNamespace(
                clob_client=_MissingMidpointClient(),
                market_ws_worker=None,
                registry=None,
            )
        )
        return await service.get_market_midpoint(token_id="token-1")

    payload = asyncio.run(run())

    assert payload is not None
    assert payload["token_id"] == "token-1"
    assert payload["midpoint"] is None
    assert payload["source"] == "rest"
