from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.infra.polymarket import market_ws_adapter
from polymarket_trader.serialization import utc_now

_decimal = market_ws_adapter.decimal_value
_first = market_ws_adapter.first_value
_parse_levels = market_ws_adapter.parse_levels
_extract_sequence = market_ws_adapter.extract_sequence
_best_price = market_ws_adapter.best_price
_worst_ask = market_ws_adapter.worst_ask


@dataclass(slots=True)
class BookState:
    snapshot: OrderbookSnapshot
    last_sequence: int | None = None
    needs_rest_snapshot: bool = False
    resolved: bool = False
    subscribed_at: datetime | None = None
    last_message_at: datetime | None = None
    last_rest_snapshot_at: datetime | None = None
    last_error: str | None = None
    last_result: object | None = None


class MarketBookProjector:
    def initial_state(self, token_id: str, market: Market | None = None) -> BookState:
        return BookState(snapshot=self.empty_snapshot(token_id, market=market))

    def empty_snapshot(self, token_id: str, *, market: Market | None = None) -> OrderbookSnapshot:
        return OrderbookSnapshot(
            token_id=token_id,
            best_bid=None,
            best_ask=None,
            bids=(),
            asks=(),
            received_at=utc_now(),
            market_slug=market.market_slug if market is not None else None,
            condition_id=market.condition_id if market is not None else None,
            tick_size=market.tick_size if market is not None else None,
        )

    def rest_state(
        self,
        token_id: str,
        snapshot: OrderbookSnapshot | Mapping[str, Any],
        *,
        market: Market | None,
        current: BookState | None,
    ) -> BookState:
        snapshot_model = (
            snapshot
            if isinstance(snapshot, OrderbookSnapshot)
            else self.snapshot_from_mapping(
                token_id,
                snapshot,
                market=market,
                previous=current.snapshot if current is not None else None,
            )
        )
        now = utc_now()
        return BookState(
            snapshot=snapshot_model,
            last_sequence=_extract_sequence(snapshot) if isinstance(snapshot, Mapping) else None,
            needs_rest_snapshot=False,
            resolved=current.resolved if current is not None else False,
            subscribed_at=current.subscribed_at if current is not None else None,
            last_message_at=now,
            last_rest_snapshot_at=now,
            last_error=current.last_error if current is not None else None,
        )

    def apply_book_message(
        self,
        token_id: str,
        state: BookState,
        message: Mapping[str, Any],
    ) -> None:
        state.snapshot = self.snapshot_from_message(token_id, message, previous=state.snapshot)
        state.needs_rest_snapshot = False

    def apply_price_message(self, state: BookState, message: Mapping[str, Any]) -> None:
        snapshot = state.snapshot
        best_bid = _decimal(_first(message, "best_bid", "bestBid", "bid"))
        best_ask = _decimal(_first(message, "best_ask", "bestAsk", "ask"))
        best_bid_size = _decimal(_first(message, "best_bid_size", "bestBidSize"))
        best_ask_size = _decimal(_first(message, "best_ask_size", "bestAskSize"))
        bids = snapshot.bids
        asks = snapshot.asks

        side = str(_first(message, "side", "book_side", "direction") or "").lower()
        price = _decimal(_first(message, "price", "new_price", "p"))
        size = _decimal(_first(message, "size", "new_size", "quantity", "qty"))
        if price is not None and size is not None:
            if side in {"bid", "buy", "yes"}:
                bids = self.upsert_level(bids, price, size)
                best_bid = best_bid or _best_price(bids)
                best_bid_size = best_bid_size or self.size_for_price(bids, best_bid)
            else:
                asks = self.upsert_level(asks, price, size)
                best_ask = best_ask or _worst_ask(asks)
                best_ask_size = best_ask_size or self.size_for_price(asks, best_ask)

        state.snapshot = replace(
            snapshot,
            best_bid=best_bid if best_bid is not None else snapshot.best_bid,
            best_ask=best_ask if best_ask is not None else snapshot.best_ask,
            best_bid_size=best_bid_size if best_bid_size is not None else snapshot.best_bid_size,
            best_ask_size=best_ask_size if best_ask_size is not None else snapshot.best_ask_size,
            bids=bids,
            asks=asks,
            received_at=utc_now(),
        )

    def apply_tick_size_change(self, state: BookState, tick_size: Decimal | None) -> None:
        state.snapshot = replace(
            state.snapshot,
            tick_size=tick_size or state.snapshot.tick_size,
            received_at=utc_now(),
        )

    def apply_last_trade_price(self, state: BookState, last_trade_price: Decimal) -> None:
        state.snapshot = replace(
            state.snapshot,
            last_trade_price=last_trade_price,
            received_at=utc_now(),
        )

    def touch(self, state: BookState) -> None:
        state.snapshot = replace(state.snapshot, received_at=utc_now())

    def snapshot_from_message(
        self,
        token_id: str,
        message: Mapping[str, Any],
        *,
        previous: OrderbookSnapshot | None = None,
    ) -> OrderbookSnapshot:
        book = _first(message, "book", "orderbook", "snapshot", "rest_snapshot")
        payload = book if isinstance(book, Mapping) else message
        bids = _parse_levels(_first(payload, "bids", "yes_bids"))
        asks = _parse_levels(_first(payload, "asks", "no_asks"))
        best_bid = _decimal(_first(payload, "best_bid", "bestBid")) or _best_price(bids)
        best_ask = _decimal(_first(payload, "best_ask", "bestAsk")) or _worst_ask(asks)
        best_bid_size = _decimal(_first(payload, "best_bid_size", "bestBidSize"))
        best_ask_size = _decimal(_first(payload, "best_ask_size", "bestAskSize"))
        if best_bid_size is None and best_bid is not None:
            best_bid_size = self.size_for_price(bids, best_bid)
        if best_ask_size is None and best_ask is not None:
            best_ask_size = self.size_for_price(asks, best_ask)
        market_slug = str(
            _first(payload, "market_slug", "marketSlug", "slug")
            or (previous.market_slug if previous else "")
        ) or None
        condition_id = str(
            _first(payload, "condition_id", "conditionId", "market")
            or (previous.condition_id if previous else "")
        ) or None
        tick_size = _decimal(_first(payload, "tick_size", "tickSize")) or (
            previous.tick_size if previous else None
        )
        last_trade_price = _decimal(_first(payload, "last_trade_price", "lastTradePrice")) or (
            previous.last_trade_price if previous else None
        )
        return OrderbookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bids,
            asks=asks,
            received_at=utc_now(),
            market_slug=market_slug,
            condition_id=condition_id,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            last_trade_price=last_trade_price,
            tick_size=tick_size,
        )

    def snapshot_from_mapping(
        self,
        token_id: str,
        payload: Mapping[str, Any],
        *,
        market: Market | None = None,
        previous: OrderbookSnapshot | None = None,
    ) -> OrderbookSnapshot:
        if previous is None and market is not None:
            previous = self.empty_snapshot(token_id, market=market)
        return self.snapshot_from_message(token_id, payload, previous=previous)

    def sequence_gap_detected(self, state: BookState, sequence: int | None) -> bool:
        if sequence is None:
            return False
        if state.last_sequence is None:
            return False
        return sequence > state.last_sequence + 1

    def upsert_level(
        self,
        levels: tuple[PriceLevel, ...],
        price: Decimal,
        size: Decimal,
    ) -> tuple[PriceLevel, ...]:
        updated: list[PriceLevel] = []
        replaced = False
        for level in levels:
            if level.price == price:
                updated.append(PriceLevel(price=price, size=size))
                replaced = True
            else:
                updated.append(level)
        if not replaced:
            updated.append(PriceLevel(price=price, size=size))
        return tuple(updated)

    def size_for_price(
        self,
        levels: tuple[PriceLevel, ...],
        price: Decimal | None,
    ) -> Decimal | None:
        if price is None:
            return None
        for level in levels:
            if level.price == price:
                return level.size
        return None

    def snapshot_payload(self, snapshot: OrderbookSnapshot) -> dict[str, Any]:
        return {
            "token_id": snapshot.token_id,
            "market_slug": snapshot.market_slug,
            "condition_id": snapshot.condition_id,
            "best_bid": self.serialize_decimal(snapshot.best_bid),
            "best_ask": self.serialize_decimal(snapshot.best_ask),
            "best_bid_size": self.serialize_decimal(snapshot.best_bid_size),
            "best_ask_size": self.serialize_decimal(snapshot.best_ask_size),
            "last_trade_price": self.serialize_decimal(snapshot.last_trade_price),
            "tick_size": self.serialize_decimal(snapshot.tick_size),
            "spread": self.serialize_decimal(snapshot.spread),
            "ask_depth": self.serialize_decimal(snapshot.buyable_ask_depth()),
            "received_at": snapshot.received_at.isoformat(),
            "snapshot_time": snapshot.received_at.isoformat(),
        }

    def serialize_decimal(self, value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value, "f")
