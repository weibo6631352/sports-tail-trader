from __future__ import annotations

import httpx
import pytest

from polymarket_trader.infra.polymarket.data_client import DataClient


class _AuthClient:
    def get_address(self) -> str:
        return "0xSigner"

    def build_l2_headers(self, *, method: str, request_path: str):
        return {"X-Test-Method": method, "X-Test-Path": request_path}


@pytest.mark.asyncio
async def test_data_client_uses_configured_trading_account_for_positions() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        base_url="https://data-api.polymarket.test",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = DataClient(
            base_url="https://data-api.polymarket.test",
            client=http_client,
            auth_client=_AuthClient(),
            default_user_address="0xFunder",
        )

        await client.list_positions()

    assert requests[0].url.params["user"] == "0xFunder"
