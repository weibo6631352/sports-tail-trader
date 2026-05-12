"""AdminMarketQueryMixin 直接 unit 测试。

覆盖：
- ``list_markets`` 从内存 registry 投影（无 DB 回退路径）+ 分页 limit/offset
- ``get_market`` 命中 / 未命中
- ``get_market_orderbook`` 热态命中 / 退化到 REST
- ``get_market_midpoint`` 用中间值；REST 404 时返回 None
- ``list_orderbook_history`` 无 DB 时返回空 page
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import pytest

from polymarket_trader.app.admin_query.market import AdminMarketQueryMixin
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.infra.polymarket import PolymarketClientError
from polymarket_trader.infra.polymarket.schemas.clob import ClobPriceHistoryDTO
from polymarket_trader.runtime.registry import MarketRegistrySnapshot


def _market(idx: int) -> Market:
    return Market(
        condition_id=f"cond-{idx}",
        market_slug=f"market-{idx}",
        outcomes=(
            MarketOutcome(token_id=f"tok-{idx}-yes", outcome="YES"),
            MarketOutcome(token_id=f"tok-{idx}-no", outcome="NO"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


@dataclass
class _FakeSerializer:
    def market_view(self, market: Market, **_: Any) -> dict[str, Any]:
        return {"condition_id": market.condition_id, "market_slug": market.market_slug}

    def market_orderbook(self, **kwargs: Any) -> dict[str, Any]:
        return {"token_id": kwargs["token_id"], "source": kwargs["source"]}

    def market_midpoint(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "token_id": kwargs["token_id"],
            "midpoint": None if kwargs["midpoint"] is None else str(kwargs["midpoint"]),
            "source": kwargs["source"],
        }

    def market_prices_history(self, **kwargs: Any) -> dict[str, Any]:
        return {"token_id": kwargs["token_id"], "interval": kwargs["interval"]}

    def orderbook(self, snap: Any) -> Any:
        return snap


@dataclass
class _FakeClob:
    midpoint: Decimal | None = Decimal("0.55")
    midpoint_raises_404: bool = False
    midpoint_raises_500: bool = False
    orderbook_snapshot: OrderbookSnapshot | None = None
    history: ClobPriceHistoryDTO = field(
        default_factory=lambda: ClobPriceHistoryDTO(raw={"history": []})
    )

    async def get_midpoint(self, token_id: str) -> Decimal | None:
        if self.midpoint_raises_404:
            raise PolymarketClientError("not found", status_code=404)
        if self.midpoint_raises_500:
            raise PolymarketClientError("server", status_code=500)
        return self.midpoint

    async def get_orderbook(self, token_id: str, **_: Any) -> Any:
        class _OB:
            def to_snapshot(self_inner) -> OrderbookSnapshot:
                return self.orderbook_snapshot or OrderbookSnapshot(
                    token_id=token_id,
                    best_bid=Decimal("0.5"),
                    best_ask=Decimal("0.6"),
                    bids=(),
                    asks=(),
                    received_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
                )

        return _OB()

    async def get_prices_history(self, token_id: str, **_: Any) -> ClobPriceHistoryDTO:
        return self.history


class _Host(AdminMarketQueryMixin):
    def __init__(
        self,
        *,
        markets: tuple[Market, ...] = (),
        has_db: bool = False,
        ws_snapshots: dict[str, OrderbookSnapshot] | None = None,
        clob: _FakeClob | None = None,
        resolved_market: Market | None = None,
    ) -> None:
        self._markets = markets
        self._has_db = has_db
        self._ws_snapshots = ws_snapshots or {}
        self._clob = clob or _FakeClob()
        self._resolved_market = resolved_market
        self._ser = _FakeSerializer()
        self._registry_snap = MarketRegistrySnapshot(markets=markets)
        self._account = AccountSnapshot()

    def _serializer(self) -> _FakeSerializer:
        return self._ser

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        return self._registry_snap

    def _account_snapshot(self) -> AccountSnapshot:
        return self._account

    def _has_db_session_factory(self) -> bool:
        return self._has_db

    def _slice_sequence(self, items: Any, *, limit: int, offset: int) -> RepositoryPage[Any]:
        items_list = tuple(items)
        sliced = items_list[offset : offset + limit]
        return RepositoryPage(items=sliced, total=len(items_list), limit=limit, offset=offset)

    def _market_ws_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        return self._ws_snapshots.get(token_id)

    def _clob_client(self) -> _FakeClob:
        return self._clob

    def _resolve_market(self, **_: Any) -> Market | None:
        return self._resolved_market

    async def _with_repositories(self, callback: Callable[[Any], Any]) -> Any:
        raise AssertionError("DB path must not be hit in unit tests without has_db=True")


def test_list_markets_paginates_in_memory_registry() -> None:
    markets = tuple(_market(i) for i in range(5))
    host = _Host(markets=markets, has_db=False)
    payload = asyncio.run(host.list_markets(limit=2, offset=1))
    assert payload["total"] == 5
    assert payload["limit"] == 2
    assert payload["offset"] == 1
    assert len(payload["items"]) == 2
    assert payload["items"][0]["condition_id"] == "cond-1"


def test_list_markets_empty_registry_returns_empty_items() -> None:
    host = _Host(markets=(), has_db=False)
    payload = asyncio.run(host.list_markets(limit=10, offset=0))
    assert payload == {"items": [], "total": 0, "limit": 10, "offset": 0}


def test_get_market_returns_none_when_unresolved_and_no_db() -> None:
    host = _Host(resolved_market=None, has_db=False)
    result = asyncio.run(host.get_market(condition_id="missing"))
    assert result is None


def test_get_market_serializes_when_resolved() -> None:
    market = _market(7)
    host = _Host(resolved_market=market, has_db=False)
    result = asyncio.run(host.get_market(condition_id=market.condition_id))
    assert result == {"condition_id": "cond-7", "market_slug": "market-7"}


def test_get_market_orderbook_returns_none_when_token_id_missing() -> None:
    host = _Host(resolved_market=None)
    result = asyncio.run(host.get_market_orderbook(condition_id="cond-x"))
    assert result is None


def test_get_market_orderbook_uses_hot_snapshot_when_present() -> None:
    market = _market(2)
    snap = OrderbookSnapshot(
        token_id="tok-2-yes",
        best_bid=Decimal("0.4"),
        best_ask=Decimal("0.6"),
        bids=(),
        asks=(),
        received_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    # 模拟非空盘口——_orderbook_has_no_quotes 为 False（best_bid/ask 都有）
    host = _Host(resolved_market=market, ws_snapshots={"tok-2-yes": snap})
    result = asyncio.run(host.get_market_orderbook(token_id="tok-2-yes"))
    assert result is not None
    assert result["source"] == "hot"


def test_get_market_orderbook_falls_back_to_rest_when_hot_empty() -> None:
    market = _market(3)
    empty_snap = OrderbookSnapshot(
        token_id="tok-3-yes",
        best_bid=None,
        best_ask=None,
        bids=(),
        asks=(),
        received_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    host = _Host(resolved_market=market, ws_snapshots={"tok-3-yes": empty_snap})
    result = asyncio.run(host.get_market_orderbook(token_id="tok-3-yes"))
    assert result is not None
    assert result["source"] == "rest"


def test_get_market_midpoint_returns_none_when_clob_404() -> None:
    clob = _FakeClob(midpoint_raises_404=True)
    host = _Host(resolved_market=None, clob=clob)
    result = asyncio.run(host.get_market_midpoint(token_id="tok-x"))
    assert result is not None
    assert result["midpoint"] is None
    assert result["source"] == "rest"


def test_get_market_midpoint_propagates_non_404_error() -> None:
    clob = _FakeClob(midpoint_raises_500=True)
    host = _Host(resolved_market=None, clob=clob)
    with pytest.raises(PolymarketClientError):
        asyncio.run(host.get_market_midpoint(token_id="tok-x"))


def test_list_orderbook_history_empty_without_db() -> None:
    host = _Host(has_db=False)
    payload = asyncio.run(host.list_orderbook_history(limit=10, offset=0, token_id="tok-x"))
    assert payload == {"items": [], "total": 0, "limit": 10, "offset": 0}
