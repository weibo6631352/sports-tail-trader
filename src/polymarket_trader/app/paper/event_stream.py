"""沙箱事件流：``ShadowEvent`` 序列 + JSONL 持久化。

ShadowEvent 携带触发一次评估所需的全量 snapshot（market / orderbook / live
metadata）。shadow_runner 顺序消费这些事件，按 observed_at 推进虚拟时钟，
每事件触发 ``TradingDecisionWorker.process_event``。

JSONL 格式说明：每行一个事件，字段 ``event_type / observed_at / condition_id /
token_id / market_slug / trace_id / market / orderbook / live_metadata``；
market 和 orderbook 用各自的 ``as_dict``-like 字典表示，shadow_runner 内部反序列化
回 ``Market`` / ``OrderbookSnapshot`` 实例。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Protocol

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel


@dataclass(frozen=True, slots=True)
class ShadowEvent:
    """单次沙箱事件：带时间戳的 market/orderbook/live 状态 snapshot。"""

    event_type: str
    observed_at: datetime
    condition_id: str
    token_id: str
    market_slug: str
    trace_id: str
    market: Market
    orderbook: OrderbookSnapshot
    live_metadata: Mapping[str, Any] = field(default_factory=dict)


class EventStreamSource(Protocol):
    """可遍历的 ShadowEvent 源。"""

    def __iter__(self) -> Iterator[ShadowEvent]: ...


@dataclass(slots=True)
class SyntheticEventStream:
    """单元测试用的 in-memory 事件流。"""

    events: tuple[ShadowEvent, ...]

    def __iter__(self) -> Iterator[ShadowEvent]:
        return iter(self.events)


@dataclass(slots=True)
class RecordedEventStream:
    """从 JSONL 文件读取 ShadowEvent；按 observed_at 升序排序后返回。"""

    path: Path

    def __iter__(self) -> Iterator[ShadowEvent]:
        events = list(load_shadow_events_from_jsonl(self.path))
        events.sort(key=lambda evt: evt.observed_at)
        return iter(events)


def dump_shadow_events_to_jsonl(events: Iterable[ShadowEvent], path: Path) -> int:
    """写出 ShadowEvent 序列到 JSONL。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as fp:
        for event in events:
            fp.write(json.dumps(_event_to_dict(event), default=str))
            fp.write("\n")
            written += 1
    return written


def load_shadow_events_from_jsonl(path: Path) -> Iterator[ShadowEvent]:
    """从 JSONL 加载 ShadowEvent；按文件顺序返回（不排序）。"""

    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            yield _event_from_dict(json.loads(line))


def _event_to_dict(event: ShadowEvent) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "observed_at": event.observed_at.isoformat(),
        "condition_id": event.condition_id,
        "token_id": event.token_id,
        "market_slug": event.market_slug,
        "trace_id": event.trace_id,
        "market": _market_to_dict(event.market),
        "orderbook": _orderbook_to_dict(event.orderbook),
        "live_metadata": dict(event.live_metadata),
    }


def _event_from_dict(payload: Mapping[str, Any]) -> ShadowEvent:
    return ShadowEvent(
        event_type=str(payload.get("event_type") or ""),
        observed_at=_parse_datetime(payload["observed_at"]),
        condition_id=str(payload.get("condition_id") or ""),
        token_id=str(payload.get("token_id") or ""),
        market_slug=str(payload.get("market_slug") or ""),
        trace_id=str(payload.get("trace_id") or ""),
        market=_market_from_dict(payload["market"]),
        orderbook=_orderbook_from_dict(payload["orderbook"]),
        live_metadata=dict(payload.get("live_metadata") or {}),
    )


def _market_to_dict(market: Market) -> dict[str, Any]:
    return {
        "condition_id": market.condition_id,
        "market_slug": market.market_slug,
        "event_slug": market.event_slug,
        "market_question": market.market_question,
        "event_title": market.event_title,
        "category": market.category,
        "tags": list(market.tags),
        "outcomes": [{"token_id": o.token_id, "outcome": o.outcome} for o in market.outcomes],
        "trading_status": market.trading_status.value,
        "fee_rate_bps": market.fee_rate_bps,
        "taker_base_fee_bps": market.taker_base_fee_bps,
        "fees_enabled": market.fees_enabled,
    }


def _market_from_dict(payload: Mapping[str, Any]) -> Market:
    outcomes = tuple(
        MarketOutcome(token_id=str(item.get("token_id") or ""), outcome=str(item.get("outcome") or ""))
        for item in payload.get("outcomes") or ()
    )
    trading_status_value = payload.get("trading_status") or TradingStatus.ELIGIBLE.value
    return Market(
        condition_id=str(payload.get("condition_id") or ""),
        market_slug=str(payload.get("market_slug") or ""),
        outcomes=outcomes,
        event_slug=payload.get("event_slug"),
        market_question=payload.get("market_question"),
        event_title=payload.get("event_title"),
        category=payload.get("category"),
        tags=tuple(payload.get("tags") or ()),
        trading_status=TradingStatus(trading_status_value),
        fee_rate_bps=payload.get("fee_rate_bps"),
        taker_base_fee_bps=payload.get("taker_base_fee_bps"),
        fees_enabled=payload.get("fees_enabled"),
    )


def _orderbook_to_dict(ob: OrderbookSnapshot) -> dict[str, Any]:
    return {
        "token_id": ob.token_id,
        "best_bid": None if ob.best_bid is None else str(ob.best_bid),
        "best_ask": None if ob.best_ask is None else str(ob.best_ask),
        "bids": [{"price": str(lvl.price), "size": str(lvl.size)} for lvl in ob.bids],
        "asks": [{"price": str(lvl.price), "size": str(lvl.size)} for lvl in ob.asks],
        "received_at": ob.received_at.isoformat(),
        "market_slug": ob.market_slug,
        "condition_id": ob.condition_id,
        "tick_size": None if ob.tick_size is None else str(ob.tick_size),
    }


def _orderbook_from_dict(payload: Mapping[str, Any]) -> OrderbookSnapshot:
    bids = tuple(
        PriceLevel(price=Decimal(str(lvl["price"])), size=Decimal(str(lvl["size"])))
        for lvl in payload.get("bids") or ()
    )
    asks = tuple(
        PriceLevel(price=Decimal(str(lvl["price"])), size=Decimal(str(lvl["size"])))
        for lvl in payload.get("asks") or ()
    )
    return OrderbookSnapshot(
        token_id=str(payload.get("token_id") or ""),
        best_bid=None if payload.get("best_bid") is None else Decimal(str(payload["best_bid"])),
        best_ask=None if payload.get("best_ask") is None else Decimal(str(payload["best_ask"])),
        bids=bids,
        asks=asks,
        received_at=_parse_datetime(payload["received_at"]),
        market_slug=payload.get("market_slug"),
        condition_id=payload.get("condition_id"),
        tick_size=None if payload.get("tick_size") is None else Decimal(str(payload["tick_size"])),
    )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
