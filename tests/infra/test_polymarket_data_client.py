from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from polymarket_trader.infra.polymarket.data_client import DataClient

pytestmark = pytest.mark.asyncio


async def test_data_client_list_positions_uses_official_query_params_and_maps_current_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/positions"
        return httpx.Response(
            200,
            json=[
                {
                    "proxyWallet": "0x56687bf447db6ffa42ffe2204a05edaa20f55839",
                    "asset": "token-1",
                    "conditionId": "0x" + "1" * 64,
                    "size": 3,
                    "avgPrice": 0.5,
                    "initialValue": 1.5,
                    "currentValue": 1.8,
                    "cashPnl": 0.3,
                    "percentPnl": 20,
                    "totalBought": 3,
                    "realizedPnl": 0.1,
                    "percentRealizedPnl": 5,
                    "curPrice": 0.6,
                    "redeemable": False,
                    "mergeable": False,
                    "title": "Sample Market A",
                    "slug": "sample-market-a",
                    "icon": "https://example.com/icon.png",
                    "eventSlug": "sample-event-a",
                    "outcome": "No",
                    "outcomeIndex": 1,
                    "oppositeOutcome": "Yes",
                    "oppositeAsset": "token-1-yes",
                    "endDate": "2026-01-01T00:00:00Z",
                    "negativeRisk": False,
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://data-api.polymarket.com",
        transport=transport,
        trust_env=False,
    ) as client:
        data = DataClient(client=client)
        positions = await data.list_positions(
            user_address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
            market_ids=("0x" + "1" * 64, "0x" + "2" * 64),
            limit=50,
            offset=10,
            sort_by="TOKENS",
            sort_direction="DESC",
        )

    request = requests[0]
    assert request.url.params.get("user") == "0x56687bf447db6ffa42ffe2204a05edaa20f55839"
    assert request.url.params.get("market") == ",".join(("0x" + "1" * 64, "0x" + "2" * 64))
    assert request.url.params.get("limit") == "50"
    assert request.url.params.get("offset") == "10"
    assert request.url.params.get("sortBy") == "TOKENS"
    assert request.url.params.get("sortDirection") == "DESC"
    assert len(positions) == 1
    position = positions[0]
    assert position.condition_id == "0x" + "1" * 64
    assert position.token_id == "token-1"
    assert position.shares == Decimal("3")
    assert position.cost_usdc == Decimal("1.5")
    assert position.market_slug == "sample-market-a"
    assert position.current_value == Decimal("1.8")
    assert position.cash_pnl == Decimal("0.3")
    assert position.total_bought == Decimal("3")
    assert position.redeemable is False
    assert position.outcome == "No"
    assert position.opposite_asset == "token-1-yes"


async def test_data_client_requires_user_and_rejects_partial_trade_filter() -> None:
    async with httpx.AsyncClient(
        base_url="https://data-api.polymarket.com",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
        trust_env=False,
    ) as client:
        data = DataClient(client=client)
        with pytest.raises(ValueError, match="user_address"):
            await data.list_positions()
