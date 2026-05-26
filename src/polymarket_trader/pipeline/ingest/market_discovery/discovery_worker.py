from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Any, Iterable, Mapping
from uuid import uuid4

from polymarket_trader.pipeline.ingest.market_discovery.ingest_service import MarketDiscoveryOutcome, MarketIngestService
from polymarket_trader.domain.discovery import RawMarketEvent
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.infra.polymarket import market_discovery_adapter
from polymarket_trader.runtime.event_bus import EventBus


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_extract_market_payloads = market_discovery_adapter.extract_market_payloads
_normalize_payload = market_discovery_adapter.normalize_payload
_payload_signature = market_discovery_adapter.payload_signature

# Gamma 长期翻页 + WS new_market 会持续推新 condition_id；缓存必须有上界，否则
# 进程驻留期内单调增长（§6 队列/缓存容量上限）。100000 远超目前体育市场池规模，
# 触顶意味着发现源出现异常或体育市场极度泛滥，应触发告警；丢弃最老一项即可，
# 因为权威源仍是 MarketIngestService.registry，缓存只用来去重 audit 噪声。
# 容量从 10000 提到 100000，避免在多查询轮转 + 长期发现窗口里被驱逐后，把
# 同一市场误识别为"首次发现"再次发 MARKET_DISCOVERED（N8）。
_MAX_CACHED_MARKETS = 100_000


def _lru_set(cache: "OrderedDict[str, RawMarketEvent]", key: str, value: RawMarketEvent) -> None:
    if key in cache:
        cache.move_to_end(key)
    cache[key] = value
    while len(cache) > _MAX_CACHED_MARKETS:
        cache.popitem(last=False)


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    source: str
    reason: str
    retry_at: datetime


@dataclass(frozen=True, slots=True)
class MarketDiscoveryEvent(DomainEvent):
    merge_key: str | None = None


class MarketDiscoveryWorker:
    priority = "P2"

    def __init__(
        self,
        *,
        market_ingest_service: MarketIngestService | None = None,
        event_bus: EventBus | None = None,
        source_name: str = "gamma",
        retry_delay_seconds: int = 30,
    ) -> None:
        if market_ingest_service is None:
            raise ValueError("market_ingest_service is required")
        self._market_service = market_ingest_service
        self._event_bus = event_bus
        self._source_name = source_name
        self._retry_delay_seconds = retry_delay_seconds
        self._trace_sequence = count()
        self._markets_by_condition_id: OrderedDict[str, RawMarketEvent] = OrderedDict()
        self._markets_by_slug: OrderedDict[str, RawMarketEvent] = OrderedDict()
        self._seen_by_condition_id: OrderedDict[str, RawMarketEvent] = OrderedDict()
        self._seen_by_slug: OrderedDict[str, RawMarketEvent] = OrderedDict()
        self._last_failure: DiscoveryFailure | None = None

    def cache_sizes(self) -> dict[str, int]:
        """缓存大小快照——供 supervisor / operator 观测 §6 容量上界。"""

        return {
            "markets_by_condition_id": len(self._markets_by_condition_id),
            "markets_by_slug": len(self._markets_by_slug),
            "seen_by_condition_id": len(self._seen_by_condition_id),
            "seen_by_slug": len(self._seen_by_slug),
            "max": _MAX_CACHED_MARKETS,
        }

    @property
    def last_failure(self) -> DiscoveryFailure | None:
        return self._last_failure

    def known_markets(self) -> tuple[RawMarketEvent, ...]:
        # 失败重试时不清空缓存，已有 markets 保持可见，避免一轮 Gamma 拉取失败就把热状态冲掉。
        if self._markets_by_condition_id:
            markets = list(self._markets_by_condition_id.values())
            seen = {market.dedupe_identity for market in markets}
            for market in self._markets_by_slug.values():
                if market.dedupe_identity not in seen:
                    markets.append(market)
                    seen.add(market.dedupe_identity)
            return tuple(markets)
        return tuple(self._markets_by_slug.values())

    def should_retry(self, now: datetime | None = None) -> bool:
        if self._last_failure is None:
            return False
        now = now or _utc_now()
        return now >= self._last_failure.retry_at

    def mark_scan_success(self) -> None:
        self._last_failure = None

    async def ingest_gamma_page(
        self,
        page: Mapping[str, Any],
        *,
        trace_id: str | None = None,
    ) -> list[DomainEvent]:
        return await self.ingest_source_page(page, source=self._source_name, trace_id=trace_id)

    async def ingest_gamma_pages(
        self,
        pages: Iterable[Mapping[str, Any]],
        *,
        trace_id: str | None = None,
    ) -> list[DomainEvent]:
        # 分页发现只要有一页成功，就可以保留已发现 markets；失败页留给 retry，不回滚热态缓存。
        events: list[DomainEvent] = []
        for page in pages:
            events.extend(await self.ingest_gamma_page(page, trace_id=trace_id))
        return events

    async def ingest_ws_new_market(
        self,
        payload: Mapping[str, Any],
        *,
        trace_id: str | None = None,
    ) -> list[DomainEvent]:
        # WS 的 new_market 是低延迟入口，但仍然必须走同一套 classifier，不能绕过分类规则。
        return await self.ingest_source_page(
            payload,
            source="market_ws.new_market",
            trace_id=trace_id,
        )

    async def ingest_source_page(
        self,
        payload: Mapping[str, Any],
        *,
        source: str,
        trace_id: str | None = None,
    ) -> list[DomainEvent]:
        discovered_trace_id = trace_id or self._next_trace_id(source)
        events: list[DomainEvent] = []
        for index, market_payload in enumerate(_extract_market_payloads(payload), start=1):
            raw_event = RawMarketEvent(
                source=source,
                payload=_normalize_payload(market_payload),
                trace_id=discovered_trace_id,
            )
            event = await self._classify_and_emit(raw_event)
            if event is not None:
                events.append(event)
            if index % 10 == 0:
                # Gamma 单页可能包含大量市场；发现链路必须定期让出事件循环，避免拖慢 Admin/API。
                await asyncio.sleep(0)
        return events

    def record_failure(
        self,
        *,
        source: str,
        reason: str,
        retry_after_seconds: int | None = None,
    ) -> DomainEvent:
        # Gamma 拉取失败时只记录重试，不清空已有 markets，也不偷偷补新市场。
        retry_seconds = (
            retry_after_seconds
            if retry_after_seconds is not None
            else self._retry_delay_seconds
        )
        retry_at = _utc_now() + timedelta(seconds=retry_seconds)
        self._last_failure = DiscoveryFailure(source=source, reason=reason, retry_at=retry_at)
        return MarketDiscoveryEvent(
            trace_id=self._next_trace_id(source),
            event_type=DomainEventType.RETRY,
            event_id=uuid4().hex,
            reason=reason,
            created_at=_utc_now(),
            merge_key=f"retry|{source}",
            payload={
                "source": source,
                "retry_at": retry_at.isoformat(),
                "reason": reason,
            },
        )

    async def _classify_and_emit(self, raw_event: RawMarketEvent) -> DomainEvent | None:
        previous = self._lookup_seen(raw_event)
        previous_signature = _payload_signature(previous.payload) if previous is not None else None
        current_signature = _payload_signature(raw_event.payload)
        unchanged_payload = previous_signature == current_signature if previous_signature is not None else False
        outcome: MarketDiscoveryOutcome = self._market_service.ingest_raw_market(
            raw_event.payload,
            source=raw_event.source,
            trace_id=raw_event.trace_id,
            discovered_at=raw_event.discovered_at,
        )
        parse_result = outcome.parse_result
        self._remember_seen(raw_event)
        if outcome.accepted:
            self._remember(raw_event)
        else:
            self._last_failure = None
        if outcome.suppress_event:
            # MarketIngestService 已对同 (cid, reason) 的 MARKET_FILTERED_OUT 高频事件去重；
            # 进入这里说明本次是冗余拒绝，直接静默，不再走首次审计路径。
            return None
        if not outcome.should_publish_event:
            # 首次看到的目标盘口必须落审计——CLAUDE.md §10「拒绝原因必须可审计」。
            # subsequent 同样市场再次发现时走原有 dedup 路径不发噪音；非目标盘口
            # （parse_result.accepted=False）依旧静默，避免审计被无关市场淹没。
            if previous is not None or not parse_result.accepted:
                return None
        previous_market = outcome.existing_market
        current_market = outcome.tracked_market or outcome.market
        if unchanged_payload and previous_market == current_market:
            return None

        # 同一 market 只能 emit 一次 MARKET_DISCOVERED；后续即便 registry 短暂丢失
        # existing_market（比如 reconcile 抹掉再补回），只要 worker 自己曾经看过，
        # 就降级为 MARKET_UPDATED，避免 audit_events 表里同一 condition_id 反复
        # 落 market_discovered（N8 实测 41049 条 vs 3586 markets）。
        event_type = outcome.event.event_type
        discovery_kind = outcome.discovery_kind
        if (
            previous is not None
            and outcome.accepted
            and event_type == DomainEventType.MARKET_DISCOVERED
        ):
            event_type = DomainEventType.MARKET_UPDATED
            discovery_kind = DomainEventType.MARKET_UPDATED.value

        event = MarketDiscoveryEvent(
            trace_id=outcome.trace_id,
            event_type=event_type,
            event_id=outcome.event.event_id,
            market_slug=outcome.event.market_slug,
            event_slug=outcome.event.event_slug,
            condition_id=outcome.event.condition_id,
            reason=outcome.event.reason,
            created_at=outcome.event.created_at,
            merge_key=raw_event.dedupe_identity,
            payload={
                "source": raw_event.source,
                "summary": raw_event.summary,
                "dedupe_key": raw_event.dedupe_key,
                "parse_status": parse_result.status.value,
                "parse_reason": parse_result.reject_reason.value if parse_result.reject_reason
                else None,
                "parse_detail": parse_result.reject_detail,
                "matched_keywords": parse_result.matched_keywords,
                "accepted": outcome.accepted,
                "strategy_reason": (
                    outcome.universe_decision.reason
                    if outcome.universe_decision is not None
                    else None
                ),
                "discovery_kind": discovery_kind,
                "market": outcome.event.payload.get("market"),
                "tracked_market": outcome.event.payload.get("tracked_market"),
                "subscription_request": outcome.subscription_request,
                "raw_market": raw_event.payload,
                "discovered_at": raw_event.discovered_at.isoformat(),
            },
        )
        if self._event_bus is not None:
            # Discovery 结果已经在当前调用内更新 registry / tracker。后续只需要审计落库，
            # 不能把批量发现事件灌进维护队列触发 reconcile 风暴。
            await self._event_bus.publish(OutboxPriority.P3, event)
        return event

    def _remember(self, raw_event: RawMarketEvent) -> None:
        if raw_event.condition_id:
            _lru_set(self._markets_by_condition_id, raw_event.condition_id, raw_event)
        if raw_event.market_slug:
            _lru_set(self._markets_by_slug, raw_event.market_slug, raw_event)
        self._last_failure = None

    def _lookup_seen(self, raw_event: RawMarketEvent) -> RawMarketEvent | None:
        if raw_event.condition_id:
            existing = self._seen_by_condition_id.get(raw_event.condition_id)
            if existing is not None:
                self._seen_by_condition_id.move_to_end(raw_event.condition_id)
                return existing
        if raw_event.market_slug:
            existing = self._seen_by_slug.get(raw_event.market_slug)
            if existing is not None:
                self._seen_by_slug.move_to_end(raw_event.market_slug)
                return existing
        return None

    def _remember_seen(self, raw_event: RawMarketEvent) -> None:
        if raw_event.condition_id:
            _lru_set(self._seen_by_condition_id, raw_event.condition_id, raw_event)
        if raw_event.market_slug:
            _lru_set(self._seen_by_slug, raw_event.market_slug, raw_event)

    def _next_trace_id(self, source: str) -> str:
        return f"{source}-{next(self._trace_sequence):08d}-{uuid4().hex}"
