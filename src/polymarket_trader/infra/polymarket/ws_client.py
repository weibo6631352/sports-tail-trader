from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from json import dumps
from typing import Any

import websockets

from polymarket_trader.infra.polymarket.base_client import (
    PolymarketClientError,
    PolymarketWebSocketError,
)
from polymarket_trader.infra.polymarket.schemas import (
    PolymarketSubscriptionChannel,
    WebSocketMessage,
    WebSocketSubscription,
    build_market_subscription_request,
    build_user_subscription_request,
    parse_ws_message,
    parse_ws_messages,
)


ReconnectHook = Callable[[int, Exception], Awaitable[None] | None]
LifecycleHook = Callable[[int], Awaitable[None] | None]


class PolymarketWebSocketClient:
    """Market Channel / User Channel WebSocket 适配器。

    这里负责订阅参数、消息解析和重连边界，不在这里做交易规则判断。
    Market Channel 必须带 `custom_feature_enabled: true`，否则 best_bid_ask / new_market / market_resolved 不会出现。
    """

    def __init__(
        self,
        *,
        market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user",
        headers: Mapping[str, str] | None = None,
        connect_timeout_s: float = 10.0,
        ping_interval_s: float = 20.0,
        ping_timeout_s: float = 20.0,
        close_timeout_s: float = 3.0,
        max_queue: int = 16,
        reconnect_delay_s: float = 5.0,
    ) -> None:
        self._market_url = market_url
        self._user_url = user_url
        self._headers = dict(headers or {})
        self._connect_timeout_s = connect_timeout_s
        self._ping_interval_s = ping_interval_s
        self._ping_timeout_s = ping_timeout_s
        self._close_timeout_s = close_timeout_s
        self._max_queue = max_queue
        self._reconnect_delay_s = reconnect_delay_s

    def build_market_subscription(self, token_ids: list[str] | tuple[str, ...]) -> WebSocketSubscription:
        return WebSocketSubscription(
            channel=PolymarketSubscriptionChannel.MARKET,
            token_ids=tuple(token_ids),
            custom_feature_enabled=True,
        )

    def build_user_subscription(
        self,
        condition_ids: list[str] | tuple[str, ...],
        *,
        auth: Mapping[str, str],
    ) -> WebSocketSubscription:
        return WebSocketSubscription(
            channel=PolymarketSubscriptionChannel.USER,
            condition_ids=tuple(condition_ids),
            auth=auth,
        )

    def build_market_subscription_request(self, token_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        return build_market_subscription_request(token_ids)

    def build_user_subscription_request(
        self,
        condition_ids: list[str] | tuple[str, ...],
        *,
        auth: Mapping[str, str],
    ) -> dict[str, Any]:
        return build_user_subscription_request(condition_ids, auth=auth)

    def parse_message(
        self,
        payload: Any,
        *,
        channel_hint: str | None = None,
    ) -> WebSocketMessage:
        return parse_ws_message(payload, channel_hint=channel_hint)

    async def stream_market_messages(
        self,
        token_ids: list[str] | tuple[str, ...],
        *,
        reconnect: bool = True,
        on_connect: LifecycleHook | None = None,
        on_disconnect: LifecycleHook | None = None,
        on_reconnect: ReconnectHook | None = None,
    ) -> AsyncIterator[WebSocketMessage]:
        async for message in self._stream(
            self._market_url,
            self.build_market_subscription_request(token_ids),
            reconnect=reconnect,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_reconnect=on_reconnect,
            channel_hint=PolymarketSubscriptionChannel.MARKET.value,
        ):
            yield message

    async def stream_user_messages(
        self,
        condition_ids: list[str] | tuple[str, ...],
        *,
        auth: Mapping[str, str],
        reconnect: bool = True,
        on_connect: LifecycleHook | None = None,
        on_disconnect: LifecycleHook | None = None,
        on_reconnect: ReconnectHook | None = None,
    ) -> AsyncIterator[WebSocketMessage]:
        async for message in self._stream(
            self._user_url,
            self.build_user_subscription_request(condition_ids, auth=auth),
            reconnect=reconnect,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_reconnect=on_reconnect,
            channel_hint=PolymarketSubscriptionChannel.USER.value,
        ):
            yield message

    async def _stream(
        self,
        uri: str,
        subscription_payload: Mapping[str, Any],
        *,
        reconnect: bool,
        on_connect: LifecycleHook | None,
        on_disconnect: LifecycleHook | None,
        on_reconnect: ReconnectHook | None,
        channel_hint: str,
    ) -> AsyncIterator[WebSocketMessage]:
        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    uri,
                    additional_headers=self._headers or None,
                    open_timeout=self._connect_timeout_s,
                    ping_interval=self._ping_interval_s,
                    ping_timeout=self._ping_timeout_s,
                    close_timeout=self._close_timeout_s,
                    max_queue=self._max_queue,
                ) as websocket:
                    if on_connect is not None:
                        await _maybe_await(on_connect(attempt))
                    await websocket.send(dumps(subscription_payload, ensure_ascii=False))
                    async for raw_message in websocket:
                        for message in parse_ws_messages(raw_message, channel_hint=channel_hint):
                            yield message
                    if on_disconnect is not None:
                        await _maybe_await(on_disconnect(attempt))
                    return
            except Exception as exc:
                normalized = _normalize_ws_error(exc, uri=uri)
                if not reconnect:
                    raise normalized from exc
                attempt += 1
                if on_reconnect is not None:
                    await _maybe_await(on_reconnect(attempt, normalized))
                await asyncio.sleep(self._reconnect_delay_s)


async def _maybe_await(value: Awaitable[None] | None) -> None:
    if value is None:
        return
    await value


def _normalize_ws_error(exc: Exception, *, uri: str) -> PolymarketClientError:
    if isinstance(exc, PolymarketClientError):
        return exc
    if isinstance(exc, TimeoutError):
        return PolymarketWebSocketError(
            f"websocket timeout: {uri}",
            operation="websocket.connect",
            url=uri,
        )
    return PolymarketWebSocketError(
        f"websocket error: {exc}",
        operation="websocket.connect",
        url=uri,
    )


__all__ = ["PolymarketWebSocketClient"]
