"""Polymarket WebSocket 消息 schema 与解析。"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from json import loads
from typing import Any
from uuid import uuid4

from polymarket_trader.infra.polymarket.base_client import (
    PolymarketWebSocketError,
    _summary,
)

from ._helpers import _first_text, _first_value, _utc_now


class PolymarketSubscriptionChannel(StrEnum):
    MARKET = "market"
    USER = "user"


@dataclass(frozen=True, slots=True)
class WebSocketSubscription:
    channel: PolymarketSubscriptionChannel
    token_ids: tuple[str, ...] = field(default_factory=tuple)
    condition_ids: tuple[str, ...] = field(default_factory=tuple)
    custom_feature_enabled: bool = False
    auth: Mapping[str, str] | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.channel.value}
        if self.channel is PolymarketSubscriptionChannel.MARKET:
            payload["assets_ids"] = list(self.token_ids)
            # Market Channel 的 best_bid_ask / new_market / market_resolved 只有开启 custom feature 才会送。
            payload["custom_feature_enabled"] = True if self.custom_feature_enabled else False
        else:
            payload["markets"] = list(self.condition_ids)
            if self.auth is not None:
                payload["auth"] = {str(key): str(value) for key, value in self.auth.items()}
        payload.update(dict(self.raw))
        return payload


@dataclass(frozen=True, slots=True)
class WebSocketMessage:
    channel: str
    message_type: str
    payload: Mapping[str, Any]
    raw: Mapping[str, Any]
    trace_id: str = ""
    token_id: str | None = None
    condition_id: str | None = None
    market_slug: str | None = None
    sequence: int | None = None
    received_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel", self.channel.strip())
        object.__setattr__(self, "message_type", self.message_type.strip().lower())
        object.__setattr__(self, "trace_id", self.trace_id.strip() if self.trace_id else uuid4().hex)
        object.__setattr__(
            self,
            "token_id",
            self.token_id
            or _first_text(self.raw, "token_id", "tokenId", "asset_id", "assetId", "winning_asset_id"),
        )
        object.__setattr__(
            self,
            "condition_id",
            self.condition_id or _first_text(self.raw, "condition_id", "conditionId", "condition", "market"),
        )
        object.__setattr__(self, "market_slug", self.market_slug or _first_text(self.raw, "market_slug", "marketSlug", "slug"))
        if self.sequence is None:
            with contextlib.suppress(Exception):
                sequence = _first_value(self.raw, "sequence", "seq", "version")
                object.__setattr__(self, "sequence", None if sequence is None else int(sequence))

    @property
    def event_type(self) -> str:
        return self.message_type


def build_market_subscription_request(token_ids: Iterable[str]) -> dict[str, Any]:
    # Market Channel 的 `best_bid_ask` / `new_market` / `market_resolved` 需要 `custom_feature_enabled: true`。
    return WebSocketSubscription(
        channel=PolymarketSubscriptionChannel.MARKET,
        token_ids=tuple(str(token_id).strip() for token_id in token_ids if str(token_id).strip()),
        custom_feature_enabled=True,
    ).to_payload()


def build_user_subscription_request(
    condition_ids: Iterable[str],
    *,
    auth: Mapping[str, str],
) -> dict[str, Any]:
    return WebSocketSubscription(
        channel=PolymarketSubscriptionChannel.USER,
        condition_ids=tuple(
            str(condition_id).strip() for condition_id in condition_ids if str(condition_id).strip()
        ),
        auth=auth,
    ).to_payload()


def parse_ws_message(
    payload: Any,
    *,
    channel_hint: str | None = None,
) -> WebSocketMessage:
    messages = parse_ws_messages(payload, channel_hint=channel_hint)
    if len(messages) != 1:
        raise PolymarketWebSocketError(
            "websocket frame contains multiple messages",
            operation="parse_ws_message",
            raw_response_summary=_summary(payload),
            raw_response=payload,
        )
    return messages[0]


def parse_ws_messages(
    payload: Any,
    *,
    channel_hint: str | None = None,
) -> tuple[WebSocketMessage, ...]:
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        payload = loads(payload)
    if isinstance(payload, list):
        messages: list[WebSocketMessage] = []
        for item in payload:
            if not isinstance(item, Mapping):
                raise PolymarketWebSocketError(
                    "websocket message is not a mapping",
                    operation="parse_ws_messages",
                    raw_response_summary=_summary(item),
                    raw_response=item,
                )
            messages.append(_parse_ws_mapping(item, channel_hint=channel_hint))
        return tuple(messages)
    if not isinstance(payload, Mapping):
        raise PolymarketWebSocketError(
            "websocket message is not a mapping",
            operation="parse_ws_messages",
            raw_response_summary=_summary(payload),
            raw_response=payload,
        )
    return (_parse_ws_mapping(payload, channel_hint=channel_hint),)


def _parse_ws_mapping(
    payload: Mapping[str, Any],
    *,
    channel_hint: str | None = None,
) -> WebSocketMessage:
    message_type = _infer_ws_message_type(payload)
    channel = channel_hint or _first_text(payload, "channel", "channel_type") or "market"

    nested_payload = _first_value(payload, "payload", "data", "message")
    if isinstance(nested_payload, Mapping):
        structured_payload = nested_payload
    else:
        structured_payload = payload

    sequence = _first_value(payload, "sequence", "seq", "version")
    sequence_value: int | None = None
    with contextlib.suppress(TypeError, ValueError):
        if sequence is not None:
            sequence_value = int(sequence)
    return WebSocketMessage(
        channel=channel,
        message_type=message_type,
        payload=structured_payload,
        raw=payload,
        token_id=_first_text(
            payload,
            "token_id",
            "tokenId",
            "asset_id",
            "assetId",
            "winning_asset_id",
        ),
        condition_id=_first_text(payload, "condition_id", "conditionId", "condition", "market"),
        market_slug=_first_text(payload, "market_slug", "marketSlug", "slug"),
        sequence=sequence_value,
    )


def _infer_ws_message_type(payload: Mapping[str, Any]) -> str:
    explicit = _first_text(
        payload,
        "event_type",
        "message_type",
        "channel_event",
        "event",
        "type",
        "action",
    )
    if explicit is not None:
        return explicit
    if isinstance(payload.get("price_changes"), list):
        return "price_change"
    if isinstance(payload.get("bids"), list) or isinstance(payload.get("asks"), list):
        return "book"
    if _first_value(payload, "best_bid", "bestBid", "best_ask", "bestAsk", "bid", "ask") is not None:
        return "best_bid_ask"
    return "message"
