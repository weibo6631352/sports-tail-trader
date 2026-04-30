from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.infra.polymarket import PolymarketClientError


class _MissingMidpointClient:
    async def get_midpoint(self, _token_id: str):
        raise PolymarketClientError(
            "midpoint not found",
            operation="clob.get_midpoint",
            status_code=404,
        )


class _OrderbookResponse:
    def __init__(self, snapshot: OrderbookSnapshot) -> None:
        self._snapshot = snapshot

    def to_snapshot(self) -> OrderbookSnapshot:
        return self._snapshot


class _RestOrderbookClient:
    def __init__(self, snapshot: OrderbookSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def get_orderbook(
        self,
        token_id: str,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
    ) -> _OrderbookResponse:
        self.calls.append((token_id, market_slug, condition_id))
        return _OrderbookResponse(self.snapshot)


class _MarketWs:
    def __init__(self, snapshot: OrderbookSnapshot | None) -> None:
        self._snapshot = snapshot

    def snapshot(self, _token_id: str) -> OrderbookSnapshot | None:
        return self._snapshot


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


def test_market_orderbook_falls_back_to_rest_when_hot_snapshot_has_no_quotes() -> None:
    async def run() -> tuple[dict[str, object] | None, _RestOrderbookClient]:
        hot_snapshot = OrderbookSnapshot(
            token_id="token-1",
            condition_id="condition-1",
            market_slug="market-1",
            best_bid=None,
            best_ask=None,
            bids=(),
            asks=(),
            received_at=datetime.now(timezone.utc),
        )
        rest_snapshot = OrderbookSnapshot(
            token_id="token-1",
            condition_id="condition-1",
            market_slug="market-1",
            best_bid=Decimal("0.52"),
            best_ask=Decimal("0.53"),
            best_bid_size=Decimal("100"),
            best_ask_size=Decimal("200"),
            bids=(PriceLevel(price=Decimal("0.52"), size=Decimal("100")),),
            asks=(PriceLevel(price=Decimal("0.53"), size=Decimal("200")),),
            received_at=datetime.now(timezone.utc),
        )
        client = _RestOrderbookClient(rest_snapshot)
        service = AdminService(
            runtime=SimpleNamespace(
                clob_client=client,
                market_ws_worker=_MarketWs(hot_snapshot),
                registry=None,
            )
        )
        return await service.get_market_orderbook(token_id="token-1"), client

    payload, client = asyncio.run(run())

    assert payload is not None
    assert payload["source"] == "rest"
    assert client.calls == [("token-1", None, None)]
    orderbook = payload["orderbook"]
    assert isinstance(orderbook, dict)
    assert orderbook["best_bid"] == "0.52"
    assert orderbook["best_ask"] == "0.53"
