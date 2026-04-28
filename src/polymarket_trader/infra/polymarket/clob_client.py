from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import httpx

from polymarket_trader.infra.polymarket.auth import PolymarketTradingClient
from polymarket_trader.infra.polymarket.base_client import PolymarketRestClientBase
from polymarket_trader.infra.polymarket.schemas import (
    BalanceAllowanceDTO,
    ClobFillDTO,
    ClobOrderDTO,
    ClobOrderRequest,
    ClobOrderbookDTO,
    ClobPriceHistoryDTO,
    normalize_balance_allowance_payload,
    normalize_fill_payload,
    normalize_order_payload,
    normalize_orderbook_payload,
    normalize_price_history_payload,
)


def _iter_mappings(payload: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(payload, list):
        return tuple(item for item in payload if isinstance(item, Mapping))
    if isinstance(payload, Mapping):
        for key in ("data", "items", "results", "rows", "orders", "fills", "trades"):
            value = payload.get(key)
            if isinstance(value, list):
                return tuple(item for item in value if isinstance(item, Mapping))
        return (payload,)
    return ()


def _normalize_request(request: ClobOrderRequest | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(request, ClobOrderRequest):
        return request.to_payload()
    return dict(request)


def _coerce_int(value: Any | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(Decimal(text))


def _coerce_decimal(value: Any | None) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Decimal(text)


def _coerce_cursor(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class ClobClient(PolymarketRestClientBase):
    """Polymarket CLOB 读写适配器。

    这里负责把 CLOB 的订单簿、订单和成交响应转换成内部 DTO。
    下单语义在适配层里明确保留：BUY 的 amount 是花费金额，SELL 的 amount 是 shares。
    """

    def __init__(
        self,
        base_url: str = "https://clob.polymarket.com",
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        headers: Mapping[str, str] | None = None,
        auth_client: PolymarketTradingClient | None = None,
        user_address: str | None = None,
        book_path: str = "/book",
        balance_allowance_path: str = "/balance-allowance",
        order_write_path: str = "/orders",
        orders_path: str = "/data/orders",
        trades_path: str = "/data/trades",
        fee_rate_path: str = "/fee-rate",
        midpoint_path: str = "/midpoint",
        prices_history_path: str = "/prices-history",
    ) -> None:
        super().__init__(base_url, client=client, timeout_s=timeout_s, headers=headers)
        self._auth_client = auth_client
        self._user_address = user_address
        self._book_path = book_path
        self._balance_allowance_path = balance_allowance_path
        self._order_write_path = order_write_path
        self._orders_path = orders_path
        self._trades_path = trades_path
        self._fee_rate_path = fee_rate_path
        self._midpoint_path = midpoint_path
        self._prices_history_path = prices_history_path

    @property
    def has_auth_client(self) -> bool:
        return self._auth_client is not None

    @property
    def default_wallet_address(self) -> str | None:
        if self._auth_client is None:
            return None
        return self._auth_client.get_address()

    async def get_orderbook(
        self,
        token_id: str,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> ClobOrderbookDTO:
        payload = await self.get_json(
            path or self._book_path,
            params={
                "token_id": token_id,
                **({"market_slug": market_slug} if market_slug is not None else {}),
                **({"condition_id": condition_id} if condition_id is not None else {}),
            },
            timeout_s=timeout_s,
            operation="clob.get_orderbook",
        )
        if isinstance(payload, Mapping):
            return normalize_orderbook_payload(
                payload,
                token_id=token_id,
                market_slug=market_slug,
                condition_id=condition_id,
            )
        raise TypeError("clob orderbook response is not a mapping")

    async def get_fee_rate(
        self,
        token_id: str,
        *,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> int:
        payload = await self.get_json(
            path or self._fee_rate_path,
            params={"token_id": token_id},
            timeout_s=timeout_s,
            operation="clob.get_fee_rate",
        )
        if not isinstance(payload, Mapping):
            raise TypeError("clob fee rate response is not a mapping")
        raw_fee_rate = payload.get("base_fee")
        if raw_fee_rate is None:
            raw_fee_rate = payload.get("baseFee")
        fee_rate_bps = _coerce_int(raw_fee_rate)
        if fee_rate_bps is None:
            raise TypeError("clob fee rate response missing base_fee")
        return fee_rate_bps

    async def get_midpoint(
        self,
        token_id: str,
        *,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> Decimal:
        payload = await self.get_json(
            path or self._midpoint_path,
            params={"token_id": token_id},
            timeout_s=timeout_s,
            operation="clob.get_midpoint",
        )
        if not isinstance(payload, Mapping):
            raise TypeError("clob midpoint response is not a mapping")
        raw_midpoint = payload.get("mid")
        if raw_midpoint is None:
            raw_midpoint = payload.get("midpoint")
        if raw_midpoint is None:
            raw_midpoint = payload.get("mid_price")
        if raw_midpoint is None:
            raw_midpoint = payload.get("midPrice")
        midpoint = _coerce_decimal(raw_midpoint)
        if midpoint is None:
            raise TypeError("clob midpoint response missing midpoint")
        return midpoint

    async def get_prices_history(
        self,
        token_id: str,
        *,
        start_ts: int | float | None = None,
        end_ts: int | float | None = None,
        interval: str | None = None,
        fidelity: int | None = None,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> ClobPriceHistoryDTO:
        params: dict[str, Any] = {"market": token_id}
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        if interval is not None:
            params["interval"] = interval
        if fidelity is not None:
            params["fidelity"] = fidelity
        payload = await self.get_json(
            path or self._prices_history_path,
            params=params,
            timeout_s=timeout_s,
            operation="clob.get_prices_history",
        )
        if isinstance(payload, Mapping):
            return normalize_price_history_payload(payload)
        raise TypeError("clob prices history response is not a mapping")

    async def get_balance_allowance(
        self,
        *,
        asset_type: str = "COLLATERAL",
        token_id: str | None = None,
        signature_type: int | None = None,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> BalanceAllowanceDTO:
        request_path = path or self._balance_allowance_path
        params: dict[str, Any] = {}
        if asset_type:
            params["asset_type"] = asset_type
        if token_id is not None:
            params["token_id"] = token_id
        resolved_signature_type = signature_type
        if resolved_signature_type is None and self._auth_client is not None:
            resolved_signature_type = self._auth_client.signature_type
        if resolved_signature_type is not None:
            params["signature_type"] = resolved_signature_type
        payload = await self.get_json(
            request_path,
            params=params,
            headers=self._auth_headers("GET", request_path),
            timeout_s=timeout_s,
            operation="clob.get_balance_allowance",
        )
        if isinstance(payload, Mapping):
            return normalize_balance_allowance_payload(payload)
        raise TypeError("clob balance allowance response is not a mapping")

    async def list_open_orders(
        self,
        *,
        order_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> tuple[ClobOrderDTO, ...]:
        params: dict[str, Any] = {}
        if order_id is not None:
            params["id"] = order_id
        if condition_id is not None:
            params["market"] = condition_id
        if token_id is not None:
            params["asset_id"] = token_id
        request_path = path or self._orders_path
        payload = await self._get_paginated_payload(
            request_path,
            params=params,
            headers=self._auth_headers("GET", request_path),
            timeout_s=timeout_s,
            operation="clob.list_open_orders",
        )
        return tuple(normalize_order_payload(item) for item in payload)

    async def list_fills(
        self,
        *,
        trade_id: str | None = None,
        maker_address: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        before: int | None = None,
        after: int | None = None,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> tuple[ClobFillDTO, ...]:
        params: dict[str, Any] = {}
        if trade_id is not None:
            params["id"] = trade_id
        if maker_address is not None:
            params["maker_address"] = maker_address
        if condition_id is not None:
            params["market"] = condition_id
        if token_id is not None:
            params["asset_id"] = token_id
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        request_path = path or self._trades_path
        payload = await self._get_paginated_payload(
            request_path,
            params=params,
            headers=self._auth_headers("GET", request_path),
            timeout_s=timeout_s,
            operation="clob.list_fills",
        )
        user_address = self._user_address or self.default_wallet_address
        return tuple(normalize_fill_payload(item, user_address=user_address) for item in payload)

    async def create_order(
        self,
        request: ClobOrderRequest | Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> ClobOrderDTO:
        payload = await self.post_json(
            path or self._order_write_path,
            json_body=_normalize_request(request),
            timeout_s=timeout_s,
            operation="clob.create_order",
        )
        if isinstance(payload, Mapping):
            return normalize_order_payload(payload)
        raise TypeError("clob create order response is not a mapping")

    async def cancel_order(
        self,
        order_id: str,
        *,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> ClobOrderDTO:
        cancel_path = path or f"{self._order_write_path.rstrip('/')}/{order_id}/cancel"
        payload = await self.post_json(
            cancel_path,
            json_body={"order_id": order_id},
            timeout_s=timeout_s,
            operation="clob.cancel_order",
        )
        if isinstance(payload, Mapping):
            return normalize_order_payload(payload)
        raise TypeError("clob cancel order response is not a mapping")

    async def replace_order(
        self,
        order_id: str,
        request: ClobOrderRequest | Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> ClobOrderDTO:
        replace_path = path or f"{self._order_write_path.rstrip('/')}/{order_id}"
        payload = await self.post_json(
            replace_path,
            json_body={"order_id": order_id, **_normalize_request(request)},
            timeout_s=timeout_s,
            operation="clob.replace_order",
        )
        if isinstance(payload, Mapping):
            return normalize_order_payload(payload)
        raise TypeError("clob replace order response is not a mapping")

    async def get_fills_for_market(
        self,
        token_id: str,
        *,
        timeout_s: float | None = None,
    ) -> tuple[ClobFillDTO, ...]:
        return await self.list_fills(token_id=token_id, timeout_s=timeout_s)

    async def _get_paginated_payload(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        operation: str,
    ) -> tuple[Mapping[str, Any], ...]:
        next_cursor: str | None = "MA=="
        items: list[Mapping[str, Any]] = []
        while True:
            page_params = dict(params or {})
            page_params["next_cursor"] = next_cursor
            payload = await self.get_json(
                path,
                params=page_params,
                headers=headers,
                timeout_s=timeout_s,
                operation=operation,
                unwrap=False,
            )
            if not isinstance(payload, Mapping):
                raise TypeError(f"{operation} response is not a mapping")
            items.extend(_iter_mappings(payload))
            next_cursor = _coerce_cursor(payload.get("next_cursor"))
            if next_cursor in {None, "", "LTE="}:
                break
        return tuple(items)

    def _auth_headers(self, method: str, request_path: str) -> Mapping[str, str] | None:
        if self._auth_client is None:
            return None
        return self._auth_client.build_l2_headers(method=method, request_path=request_path)


__all__ = ["ClobClient"]
