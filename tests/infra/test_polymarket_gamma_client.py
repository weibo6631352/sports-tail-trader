from __future__ import annotations

import httpx
import pytest

from polymarket_trader.infra.polymarket.gamma_client import GammaClient

pytestmark = pytest.mark.asyncio


async def test_gamma_client_list_events_by_params_passes_through_official_query_params() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/events"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "event-1",
                    "slug": "sample-event-a",
                    "title": "Sample Event A",
                    "active": True,
                    "closed": False,
                    "markets": [
                        {
                            "conditionId": "condition-1",
                            "slug": "sample-market-a",
                            "eventSlug": "sample-event-a",
                            "eventTitle": "Sample Event A",
                            "question": "Sample question?",
                            "clobTokenIds": "[\"yes-1\",\"no-1\"]",
                            "orderPriceMinTickSize": "0.01",
                            "orderMinSize": "1",
                        }
                    ],
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://gamma-api.polymarket.com",
        transport=transport,
        trust_env=False,
    ) as client:
        gamma = GammaClient(client=client)
        events = await gamma.list_events_by_params(
            {
                "active": True,
                "closed": False,
                "tag_slug": "crypto",
                "title_search": "threshold",
                "limit": 50,
                "offset": 10,
            }
        )

    request = requests[0]
    assert request.url.params.get("active") == "true"
    assert request.url.params.get("closed") == "false"
    assert request.url.params.get("tag_slug") == "crypto"
    assert request.url.params.get("title_search") == "threshold"
    assert request.url.params.get("limit") == "50"
    assert request.url.params.get("offset") == "10"
    assert len(events) == 1
    assert events[0].event_slug == "sample-event-a"


async def test_gamma_client_list_events_keyset_by_params_returns_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/events/keyset"
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "id": "event-1",
                        "slug": "sample-event-a",
                        "title": "Sample Event A",
                        "active": True,
                        "closed": False,
                        "markets": [],
                    }
                ],
                "next_cursor": "cursor-2",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://gamma-api.polymarket.com",
        transport=transport,
        trust_env=False,
    ) as client:
        gamma = GammaClient(client=client)
        events, next_cursor = await gamma.list_events_keyset_by_params(
            {
                "active": True,
                "closed": False,
                "title_search": "threshold",
                "limit": 100,
            }
        )

    request = requests[0]
    assert request.url.params.get("title_search") == "threshold"
    assert request.url.params.get("limit") == "100"
    assert len(events) == 1
    assert next_cursor == "cursor-2"


async def test_gamma_client_list_markets_by_params_passes_through_query_params() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/markets"
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": "condition-1",
                    "slug": "sample-market-a",
                    "eventSlug": "sample-event-a",
                    "eventTitle": "Sample Event A",
                    "question": "Sample question?",
                    "clobTokenIds": ["yes-1", "no-1"],
                    "orderPriceMinTickSize": "0.01",
                    "orderMinSize": "1",
                    "takerBaseFee": 1000,
                    "feeSchedule": {
                        "rate": 0.072,
                        "takerOnly": True,
                    },
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://gamma-api.polymarket.com",
        transport=transport,
        trust_env=False,
    ) as client:
        gamma = GammaClient(client=client)
        markets = await gamma.list_markets_by_params(
            {
                "slug": ["sample-market-a"],
                "limit": 25,
                "offset": 5,
            }
        )

    request = requests[0]
    assert request.url.params.get_list("slug") == ["sample-market-a"]
    assert request.url.params.get("limit") == "25"
    assert request.url.params.get("offset") == "5"
    assert len(markets) == 1
    assert markets[0].market_slug == "sample-market-a"
    assert markets[0].taker_base_fee_bps == 72
    assert markets[0].to_market().fee_rate_bps == 72


async def test_gamma_client_list_markets_keyset_by_params_returns_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/markets/keyset"
        return httpx.Response(
            200,
            json={
                "markets": [
                    {
                        "conditionId": "condition-1",
                        "slug": "sample-market-a",
                        "eventSlug": "sample-event-a",
                        "eventTitle": "Sample Event A",
                        "question": "Sample question?",
                        "clobTokenIds": ["yes-1", "no-1"],
                        "orderPriceMinTickSize": "0.01",
                        "orderMinSize": "1",
                        "active": True,
                        "closed": False,
                    }
                ],
                "next_cursor": "cursor-2",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://gamma-api.polymarket.com",
        transport=transport,
        trust_env=False,
    ) as client:
        gamma = GammaClient(client=client)
        markets, next_cursor = await gamma.list_markets_keyset_by_params(
            {
                "active": True,
                "closed": False,
                "limit": 100,
            }
        )

    request = requests[0]
    assert request.url.params.get("active") == "true"
    assert request.url.params.get("closed") == "false"
    assert request.url.params.get("limit") == "100"
    assert len(markets) == 1
    assert markets[0].market_slug == "sample-market-a"
    assert next_cursor == "cursor-2"


async def test_gamma_client_get_public_profile_by_wallet_address() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/public-profile"
        return httpx.Response(
            200,
            json={
                "proxyWallet": "0x2222222222222222222222222222222222222222",
                "profileImage": "https://example.com/avatar.png",
                "displayUsernamePublic": True,
                "pseudonym": "Poly-2222",
                "name": "FDV Trader",
                "xUsername": "fdv_trader",
                "verifiedBadge": True,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://gamma-api.polymarket.com",
        transport=transport,
        trust_env=False,
    ) as client:
        gamma = GammaClient(client=client)
        profile = await gamma.get_public_profile("0x2222222222222222222222222222222222222222")

    request = requests[0]
    assert request.url.params.get("address") == "0x2222222222222222222222222222222222222222"
    assert profile.proxy_wallet == "0x2222222222222222222222222222222222222222"
    assert profile.profile_image == "https://example.com/avatar.png"
    assert profile.name == "FDV Trader"
    assert profile.pseudonym == "Poly-2222"
    assert profile.x_username == "fdv_trader"
    assert profile.verified_badge is True


async def test_gamma_client_reuses_public_profile_by_wallet_address() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "proxyWallet": "0x2222222222222222222222222222222222222222",
                "profileImage": "https://example.com/avatar.png",
                "displayUsernamePublic": True,
                "pseudonym": "Poly-2222",
                "name": "FDV Trader",
                "xUsername": "fdv_trader",
                "verifiedBadge": True,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://gamma-api.polymarket.com",
        transport=transport,
        trust_env=False,
    ) as client:
        gamma = GammaClient(client=client)
        first = await gamma.get_public_profile("0x2222222222222222222222222222222222222222")
        second = await gamma.get_public_profile("0x2222222222222222222222222222222222222222")

    assert len(requests) == 1
    assert first is second
