from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.infra.polymarket import market_ws_adapter
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from .book_projector import BookState as _BookState
from .book_projector import MarketBookProjector
from .market_updater import MarketWsMarketUpdater


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_decimal = market_ws_adapter.decimal_value
_first = market_ws_adapter.first_value
_to_datetime = market_ws_adapter.datetime_value
_extract_token_id = market_ws_adapter.extract_token_id
_extract_sequence = market_ws_adapter.extract_sequence
_message_type = market_ws_adapter.message_type
_extract_token_candidates = market_ws_adapter.extract_token_candidates


def _snapshot_has_quotes(snapshot: OrderbookSnapshot) -> bool:
    return (
        snapshot.best_bid is not None
        or snapshot.best_ask is not None
        or bool(snapshot.bids)
        or bool(snapshot.asks)
    )


@dataclass(frozen=True, slots=True)
class MarketWsResultSummary:
    token_id: str
    market_slug: str | None
    condition_id: str | None
    source: str
    reason: str
    event_types: tuple[str, ...]
    last_sequence: int | None
    resolved: bool
    needs_rest_snapshot: bool
    best_bid: Decimal | None
    best_ask: Decimal | None
    ask_depth: Decimal
    created_at: datetime = field(default_factory=_utc_now)


@dataclass(frozen=True, slots=True)
class MarketWsSubscriptionStatus:
    token_id: str
    market_slug: str | None
    condition_id: str | None
    subscribed_at: datetime | None
    last_message_at: datetime | None
    last_rest_snapshot_at: datetime | None
    last_sequence: int | None
    resolved: bool
    needs_rest_snapshot: bool
    last_error: str | None


@dataclass(frozen=True, slots=True)
class MarketWsWorkerStatus:
    tracked_market_count: int
    subscription_count: int
    # connected 反映 WS lifecycle 真实状态：握手成功后 on_connect 置 True，
    # on_disconnect/on_reconnect 置 False。worker 在 starting 阶段从未握手时
    # 必须为 False，否则 supervisor.readiness 会误把"未连接"当成"连接已就绪"。
    connected: bool
    tracked_token_ids: tuple[str, ...]
    subscribed_token_ids: tuple[str, ...]
    resolved_token_ids: tuple[str, ...]
    needs_rest_snapshot_token_ids: tuple[str, ...]
    last_message_at: datetime | None
    last_rest_snapshot_at: datetime | None
    last_error: str | None
    last_result: MarketWsResultSummary | None
    recent_results: tuple[MarketWsResultSummary, ...]
    subscriptions: tuple[MarketWsSubscriptionStatus, ...]


@dataclass(frozen=True, slots=True)
class MarketWsEvent(DomainEvent):
    merge_key: str | None = None


class MarketWsWorker:
    priority = "P0"

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        registry: MarketRegistry | None = None,
        rest_snapshot_loader: Callable[
            [str],
            Awaitable[OrderbookSnapshot | Mapping[str, Any]],
        ]
        | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._registry = registry
        self._rest_snapshot_loader = rest_snapshot_loader
        self._book_projector = MarketBookProjector()
        self._states: dict[str, _BookState] = {}
        self._tracked_markets: dict[str, Market] = {}
        self._market_updater = MarketWsMarketUpdater(
            registry=registry,
            tracked_markets=self._tracked_markets,
            serialize_decimal=self._book_projector.serialize_decimal,
        )
        self._recent_results: deque[MarketWsResultSummary] = deque(maxlen=8)
        self._last_message_at: datetime | None = None
        self._last_rest_snapshot_at: datetime | None = None
        self._last_error: str | None = None
        # ws_loops 的 on_connect/on_disconnect/on_reconnect lifecycle hook 维护
        # 这个标志。未握手前一直为 False，避免"无 last_error 即视为已连接"误报。
        self._is_connected: bool = False

    def track_market(self, market: Market) -> None:
        tracked_token_ids = market.token_ids
        for token_id in tracked_token_ids:
            self._tracked_markets[token_id] = market
        if self._registry is not None:
            self._registry.upsert(market)
        for token_id in tracked_token_ids:
            if token_id in self._states:
                continue
            self._states[token_id] = self._book_projector.initial_state(token_id, market=market)

    def untrack_market(self, token_ids: str | tuple[str, ...] | list[str]) -> None:
        normalized_token_ids = (
            tuple(str(item).strip() for item in token_ids if str(item).strip())
            if isinstance(token_ids, (list, tuple))
            else ((str(token_ids).strip(),) if str(token_ids).strip() else ())
        )
        if not normalized_token_ids:
            return
        market = next(
            (
                self._tracked_markets.get(token_id)
                for token_id in normalized_token_ids
                if self._tracked_markets.get(token_id) is not None
            ),
            None,
        )
        tracked_token_ids = (
            market.token_ids
            if market is not None
            else normalized_token_ids
        )
        for tracked_token_id in tracked_token_ids:
            self._tracked_markets.pop(tracked_token_id, None)
            self._states.pop(tracked_token_id, None)

    def build_subscription_request(
        self,
        token_ids: str | tuple[str, ...] | list[str],
    ) -> dict[str, Any]:
        normalized_token_ids = (
            tuple(str(item).strip() for item in token_ids if str(item).strip())
            if isinstance(token_ids, (list, tuple))
            else ((str(token_ids).strip(),) if str(token_ids).strip() else ())
        )
        for token_id in normalized_token_ids:
            self._mark_subscribed(token_id)
        return {
            "assets_ids": list(normalized_token_ids),
            "type": "market",
            "custom_feature_enabled": True,
        }

    async def handle_message(
        self,
        message: Mapping[str, Any],
        *,
        source: str = "market_ws",
    ) -> list[DomainEvent]:
        self._last_message_at = _utc_now()
        message_type = _message_type(message)
        if message_type == "price_change" and isinstance(message.get("price_changes"), list):
            expanded_events: list[DomainEvent] = []
            for item in message["price_changes"]:
                if not isinstance(item, Mapping):
                    continue
                expanded = dict(message)
                expanded.pop("price_changes", None)
                expanded.update(item)
                expanded["event_type"] = "price_change"
                expanded_events.extend(await self.handle_message(expanded, source=source))
            return expanded_events

        token_id = self._resolve_token_id(message)
        if token_id is None:
            return []

        market = self._resolve_market(message, token_id)
        state = self._ensure_state(token_id, market)
        state.last_error = None
        self._last_error = None
        if state.resolved and message_type != "market_resolved":
            return []

        sequence = _extract_sequence(message)
        if self._book_projector.sequence_gap_detected(state, sequence):
            state.needs_rest_snapshot = True
            if self._rest_snapshot_loader is not None:
                snapshot = await self._rest_snapshot_loader(token_id)
                return await self.apply_rest_snapshot(token_id, snapshot, source=source)
            self.record_error("rest_snapshot_loader_unavailable", token_id=token_id)
            return []

        if state.needs_rest_snapshot and message_type not in {"rest_snapshot", "snapshot", "book", "orderbook"}:
            return []
        events: list[DomainEvent] = []

        if message_type in {"book", "orderbook", "snapshot", "rest_snapshot"}:
            events.extend(await self._apply_book_message(token_id, state, message, source=source))
        elif message_type in {"price_change", "best_bid_ask", "best_bidask", "best_bid_and_ask"}:
            events.extend(await self._apply_price_message(token_id, state, message, source=source))
        elif message_type == "tick_size_change":
            events.extend(
                await self._apply_tick_size_change(
                    token_id,
                    state,
                    market,
                    message,
                    source=source,
                )
            )
        elif message_type == "last_trade_price":
            events.extend(
                await self._apply_last_trade_price(
                    token_id,
                    state,
                    market,
                    message,
                    source=source,
                )
            )
        elif message_type == "market_resolved":
            events.extend(
                await self._apply_market_resolved(token_id, state, message, source=source)
            )
        elif message_type == "new_market":
            events.extend(
                await self._apply_market_metadata(
                    token_id,
                    state,
                    market,
                    message,
                    source=source,
                )
            )
        else:
            # 高频 WS 里会有不少和 orderbook 无关的 control message，这里只忽略它们。
            return []

        state.last_sequence = sequence if sequence is not None else state.last_sequence
        return events

    async def apply_rest_snapshot(
        self,
        token_id: str,
        snapshot: OrderbookSnapshot | Mapping[str, Any],
        *,
        source: str = "rest_snapshot",
    ) -> list[DomainEvent]:
        self._last_rest_snapshot_at = _utc_now()
        market = self._tracked_markets.get(token_id) or self._resolve_market({}, token_id)
        current = self._states.get(token_id)
        self._states[token_id] = self._book_projector.rest_state(
            token_id,
            snapshot,
            market=market,
            current=current,
        )
        return await self._emit_snapshot_update(
            token_id,
            self._states[token_id],
            source=source,
            reason="rest_snapshot",
        )

    async def refresh_rest_snapshots(self, token_ids: tuple[str, ...] | list[str]) -> int:
        """为刚进入订阅集合但仍为空的热态盘口预取 REST 快照。

        该方法只把权威 REST 盘口写回 Market WS worker 的热态缓存，并发布正常的
        orderbook snapshot 事件；交易决策仍然只读取统一热态，不新增下单旁路。
        """

        if self._rest_snapshot_loader is None:
            return 0
        refreshed = 0
        for token_id in tuple(str(item).strip() for item in token_ids if str(item).strip()):
            state = self._states.get(token_id)
            if state is not None and _snapshot_has_quotes(state.snapshot):
                continue
            try:
                snapshot = await self._rest_snapshot_loader(token_id)
            except Exception as exc:
                self.record_error(str(exc), token_id=token_id)
                continue
            await self.apply_rest_snapshot(token_id, snapshot, source="subscription_rest_prefetch")
            refreshed += 1
        return refreshed

    def snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        state = self._states.get(token_id)
        return None if state is None else state.snapshot

    def status_snapshot(self, *, include_subscriptions: bool = True) -> MarketWsWorkerStatus:
        if not include_subscriptions:
            recent_results = tuple(self._recent_results)
            # tracked_market_count 是已注册关注的 token 总数；
            # subscription_count 只计算真正通过 WS 订阅成功（state.subscribed_at 非空）的 token，
            # 否则 metrics 端 subscribed_count 会被"仅注册未订阅"的 token 污染（F6）。
            subscription_count = sum(
                1 for state in self._states.values() if state.subscribed_at is not None
            )
            return MarketWsWorkerStatus(
                tracked_market_count=len(self._tracked_markets),
                subscription_count=subscription_count,
                connected=self._is_connected,
                tracked_token_ids=(),
                subscribed_token_ids=(),
                resolved_token_ids=(),
                needs_rest_snapshot_token_ids=(),
                last_message_at=self._last_message_at,
                last_rest_snapshot_at=self._last_rest_snapshot_at,
                last_error=self._last_error,
                last_result=recent_results[-1] if recent_results else None,
                recent_results=recent_results,
                subscriptions=(),
            )

        subscriptions: list[MarketWsSubscriptionStatus] = []
        tracked_token_ids = tuple(sorted(self._tracked_markets.keys()))
        subscribed_token_ids: list[str] = []
        resolved_token_ids: list[str] = []
        needs_rest_snapshot_token_ids: list[str] = []
        last_error: str | None = self._last_error
        last_result: MarketWsResultSummary | None = None
        last_message_at = self._last_message_at
        last_rest_snapshot_at = self._last_rest_snapshot_at

        for token_id in tracked_token_ids:
            state = self._states.get(token_id)
            if state is None:
                continue
            if state.subscribed_at is not None:
                subscribed_token_ids.append(token_id)
            if state.resolved:
                resolved_token_ids.append(token_id)
            if state.needs_rest_snapshot:
                needs_rest_snapshot_token_ids.append(token_id)
            if state.last_error is not None and last_error is None:
                last_error = state.last_error
            state_result = state.last_result
            if isinstance(state_result, MarketWsResultSummary) and (
                last_result is None or state_result.created_at > last_result.created_at
            ):
                last_result = state_result
            subscriptions.append(
                MarketWsSubscriptionStatus(
                    token_id=token_id,
                    market_slug=state.snapshot.market_slug,
                    condition_id=state.snapshot.condition_id,
                    subscribed_at=state.subscribed_at,
                    last_message_at=state.last_message_at,
                    last_rest_snapshot_at=state.last_rest_snapshot_at,
                    last_sequence=state.last_sequence,
                    resolved=state.resolved,
                    needs_rest_snapshot=state.needs_rest_snapshot,
                    last_error=state.last_error,
                )
            )
            if state.last_message_at is not None and (
                last_message_at is None or state.last_message_at > last_message_at
            ):
                last_message_at = state.last_message_at
            if state.last_rest_snapshot_at is not None and (
                last_rest_snapshot_at is None or state.last_rest_snapshot_at > last_rest_snapshot_at
            ):
                last_rest_snapshot_at = state.last_rest_snapshot_at

        return MarketWsWorkerStatus(
            tracked_market_count=len(tracked_token_ids),
            subscription_count=len(subscribed_token_ids),
            connected=self._is_connected,
            tracked_token_ids=tracked_token_ids,
            subscribed_token_ids=tuple(subscribed_token_ids),
            resolved_token_ids=tuple(resolved_token_ids),
            needs_rest_snapshot_token_ids=tuple(needs_rest_snapshot_token_ids),
            last_message_at=last_message_at,
            last_rest_snapshot_at=last_rest_snapshot_at,
            last_error=last_error,
            last_result=last_result,
            recent_results=tuple(self._recent_results),
            subscriptions=tuple(subscriptions),
        )

    def record_error(self, reason: str, *, token_id: str | None = None) -> None:
        self._last_error = reason
        if token_id is not None:
            state = self._states.get(token_id)
            if state is not None:
                state.last_error = reason

    def clear_error(self) -> None:
        """连接恢复或收到有效盘口后清除全局错误，避免陈旧错误阻断 readiness。"""

        self._last_error = None

    def set_connection_state(self, connected: bool) -> None:
        """由 ws_loops 在 lifecycle hook 中切换真实连接状态。

        readiness 计算严格依赖这个标志：starting 阶段未握手时必须为 False，
        否则 supervisor 会把"未连接"当成"已连接"误开闸（CLAUDE.md §10）。
        """

        self._is_connected = bool(connected)

    def buyable_depth(self, token_id: str, price_limit: Decimal | None = None) -> Decimal:
        state = self._states.get(token_id)
        if state is None:
            return Decimal("0")
        return state.snapshot.buyable_ask_depth(price_limit)

    async def _apply_book_message(
        self,
        token_id: str,
        state: _BookState,
        message: Mapping[str, Any],
        *,
        source: str,
    ) -> list[DomainEvent]:
        self._book_projector.apply_book_message(token_id, state, message)
        return await self._emit_snapshot_update(token_id, state, source=source, reason="book")

    async def _apply_price_message(
        self,
        token_id: str,
        state: _BookState,
        message: Mapping[str, Any],
        *,
        source: str,
    ) -> list[DomainEvent]:
        self._book_projector.apply_price_message(state, message)
        return await self._emit_snapshot_update(
            token_id,
            state,
            source=source,
            reason="price_change",
        )

    async def _apply_tick_size_change(
        self,
        token_id: str,
        state: _BookState,
        market: Market | None,
        message: Mapping[str, Any],
        *,
        source: str,
    ) -> list[DomainEvent]:
        tick_size = _decimal(_first(message, "tick_size", "tickSize", "new_tick_size"))
        if tick_size is not None and market is not None and self._registry is not None:
            # tick size 变化要回写 Market Registry，热态路由走内存对象，不碰数据库。
            try:
                self._registry.change_tick_size(market.condition_id, tick_size)
            except AttributeError:
                self._registry.upsert(replace(market, tick_size=tick_size))
            market = replace(market, tick_size=tick_size)
            self._tracked_markets[token_id] = market
        self._book_projector.apply_tick_size_change(state, tick_size)
        return await self._emit_snapshot_update(
            token_id,
            state,
            source=source,
            reason="tick_size_change",
        )

    async def _apply_last_trade_price(
        self,
        token_id: str,
        state: _BookState,
        market: Market | None,
        message: Mapping[str, Any],
        *,
        source: str,
    ) -> list[DomainEvent]:
        last_trade_price = _decimal(_first(message, "last_trade_price", "lastTradePrice", "price"))
        if last_trade_price is None:
            return []
        events: list[DomainEvent] = []
        fee_rate_bps = self._market_updater.fee_rate_bps_from_message(message)
        updated_at = _to_datetime(_first(message, "timestamp", "updated_at", "updatedAt")) or _utc_now()
        if market is not None and fee_rate_bps is not None:
            updated_market = self._market_updater.update_fee_rate(
                token_id,
                market,
                fee_rate_bps,
                updated_at=updated_at,
            )
            if updated_market is not None:
                market = updated_market
                events.append(
                    await self._publish_market_update(
                        updated_market,
                        source=source,
                        reason="last_trade_price",
                        message=message,
                    )
                )
        self._book_projector.apply_last_trade_price(state, last_trade_price)
        events.extend(
            await self._emit_snapshot_update(
                token_id,
                state,
                source=source,
                reason="last_trade_price",
            )
        )
        return events

    async def _apply_market_resolved(
        self,
        token_id: str,
        state: _BookState,
        message: Mapping[str, Any],
        *,
        source: str,
    ) -> list[DomainEvent]:
        state.resolved = True
        self._book_projector.touch(state)
        market = self._tracked_markets.get(token_id)
        if market is not None and self._registry is not None:
            resolved_market = self._registry.mark_resolved(market.condition_id)
            if resolved_market is not None:
                for tracked_token_id in resolved_market.token_ids:
                    self._tracked_markets[tracked_token_id] = resolved_market
                    tracked_state = self._states.get(tracked_token_id)
                    if tracked_state is not None:
                        tracked_state.resolved = True
                        self._book_projector.touch(tracked_state)
        event = MarketWsEvent(
            trace_id=uuid4().hex,
            event_type=DomainEventType.MARKET_RESOLVED_OR_DISABLED,
            event_id=uuid4().hex,
            token_id=token_id,
            market_slug=state.snapshot.market_slug,
            condition_id=state.snapshot.condition_id,
            reason=str(_first(message, "reason", "status") or "market_resolved"),
            created_at=state.snapshot.received_at,
            merge_key=f"market_resolved_or_disabled|{token_id}",
            payload={
                "source": source,
                "snapshot": self._book_projector.snapshot_payload(state.snapshot),
                "message": dict(message),
            },
        )
        return [await self._publish(OutboxPriority.P0, event)]

    async def _apply_market_metadata(
        self,
        token_id: str,
        state: _BookState,
        market: Market | None,
        message: Mapping[str, Any],
        *,
        source: str,
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        if market is not None:
            updated_market = self._market_updater.update_fee_schedule(token_id, market, message)
            if updated_market is not None:
                market = updated_market
                events.append(
                    await self._publish_market_update(
                        updated_market,
                        source=source,
                        reason="new_market",
                        message=message,
                    )
                )
            elif self._registry is not None:
                self._registry.upsert(market)
        self._book_projector.touch(state)
        events.extend(
            await self._emit_snapshot_update(token_id, state, source=source, reason="new_market")
        )
        return events

    async def _emit_snapshot_update(
        self,
        token_id: str,
        state: _BookState,
        *,
        source: str,
        reason: str,
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        snapshot = state.snapshot
        event = MarketWsEvent(
            trace_id=uuid4().hex,
            event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
            event_id=uuid4().hex,
            token_id=token_id,
            market_slug=snapshot.market_slug,
            condition_id=snapshot.condition_id,
            reason=reason,
            created_at=snapshot.received_at,
            merge_key=f"orderbook_snapshot_updated|{token_id}",
            payload={
                "source": source,
                "snapshot": self._book_projector.snapshot_payload(snapshot),
                "spread": self._book_projector.serialize_decimal(snapshot.spread),
                "ask_depth": self._book_projector.serialize_decimal(snapshot.buyable_ask_depth()),
                "snapshot_time": snapshot.snapshot_time.isoformat(),
                "needs_rest_snapshot": state.needs_rest_snapshot,
            },
        )
        events.append(await self._publish(OutboxPriority.P1, event))
        self._record_result(
            token_id,
            state,
            source=source,
            reason=reason,
            event_types=tuple(str(event.event_type) for event in events),
        )
        return events

    async def _publish(self, priority: OutboxPriority, event: DomainEvent) -> DomainEvent:
        if self._event_bus is not None:
            await self._event_bus.publish(priority, event)
        return event

    def _record_result(
        self,
        token_id: str,
        state: _BookState,
        *,
        source: str,
        reason: str,
        event_types: tuple[str, ...],
    ) -> None:
        snapshot = state.snapshot
        result = MarketWsResultSummary(
            token_id=token_id,
            market_slug=snapshot.market_slug,
            condition_id=snapshot.condition_id,
            source=source,
            reason=reason,
            event_types=event_types,
            last_sequence=state.last_sequence,
            resolved=state.resolved,
            needs_rest_snapshot=state.needs_rest_snapshot,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            ask_depth=snapshot.buyable_ask_depth(),
        )
        state.last_message_at = snapshot.received_at
        state.last_result = result
        if reason in {"rest_snapshot", "reconcile_rest"}:
            state.last_rest_snapshot_at = snapshot.received_at
            self._last_rest_snapshot_at = snapshot.received_at
        self._last_message_at = snapshot.received_at
        self._recent_results.append(result)

    def _mark_subscribed(self, token_id: str) -> None:
        state = self._states.get(token_id)
        if state is None:
            return
        if state.subscribed_at is None:
            state.subscribed_at = _utc_now()

    def _ensure_state(self, token_id: str, market: Market | None) -> _BookState:
        state = self._states.get(token_id)
        if state is not None:
            return state
        state = self._book_projector.initial_state(token_id, market=market)
        self._states[token_id] = state
        return state

    def _resolve_token_id(self, message: Mapping[str, Any]) -> str | None:
        candidates = _extract_token_candidates(message)
        if not candidates:
            return None
        for token_id in candidates:
            if token_id in self._tracked_markets or token_id in self._states:
                return token_id
            if self._registry is not None and self._registry.get_by_token_id(token_id) is not None:
                return token_id
        return candidates[0]

    def _resolve_market(self, message: Mapping[str, Any], token_id: str) -> Market | None:
        if token_id in self._tracked_markets:
            return self._tracked_markets[token_id]
        condition_id = _first(message, "condition_id", "conditionId", "market")
        market_slug = _first(message, "market_slug", "marketSlug", "slug")
        if self._registry is not None:
            if condition_id:
                market = self._registry.get_by_condition_id(str(condition_id))
                if market is not None:
                    self._tracked_markets[token_id] = market
                    return market
            market = self._registry.get_by_token_id(token_id)
            if market is not None:
                self._tracked_markets[token_id] = market
                return market
            if market_slug:
                market = self._registry.get_by_slug(str(market_slug))
                if market is not None:
                    self._tracked_markets[token_id] = market
                    return market
        return None

    async def _publish_market_update(
        self,
        market: Market,
        *,
        source: str,
        reason: str,
        message: Mapping[str, Any],
    ) -> DomainEvent:
        event = MarketWsEvent(
            trace_id=uuid4().hex,
            event_type=DomainEventType.MARKET_UPDATED,
            event_id=uuid4().hex,
            token_id=None,
            market_slug=market.market_slug,
            condition_id=market.condition_id,
            reason=reason,
            created_at=_utc_now(),
            merge_key=f"market_updated|{market.condition_id}",
            payload={
                "source": source,
                "market": self._market_updater.market_payload(market),
                "message": dict(message),
            },
        )
        return await self._publish(OutboxPriority.P2, event)
