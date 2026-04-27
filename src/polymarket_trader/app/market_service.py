from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4

from polymarket_trader.app.market_payload_parser import MarketParseResult, MarketPayloadParser
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.market import Market
from polymarket_trader.observability.trace import ensure_trace_id
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.extension_api import ExtensionHooks, UniverseDecision

AccountSnapshotProvider = Callable[[], AccountSnapshot]


class MarketService:
    """Coordinates market discovery, extension universe filtering, and registry updates."""

    def __init__(
        self,
        *,
        extension_hooks: ExtensionHooks,
        parser: MarketPayloadParser | None = None,
        registry: MarketRegistry | None = None,
        market_tracker: Any | None = None,
        account_snapshot_provider: AccountSnapshotProvider | None = None,
    ) -> None:
        self._parser = parser or MarketPayloadParser()
        self._extension_hooks = extension_hooks
        self._registry = registry
        self._market_tracker = market_tracker
        self._account_snapshot_provider = account_snapshot_provider

    def ingest_raw_market(
        self,
        raw_market: Mapping[str, Any],
        *,
        source: str,
        trace_id: str | None = None,
        discovered_at: datetime | None = None,
    ) -> "MarketDiscoveryOutcome":
        trace_id = trace_id or ensure_trace_id()
        discovered_at = discovered_at or datetime.now(timezone.utc)
        parse_result = self._parser.parse(raw_market)
        existing_market = self._lookup_existing_market(parse_result)
        account_snapshot = self._current_account_snapshot()

        market: Market | None = None
        tracked_market: Market | None = None
        tracking_retained = False
        subscription_request: dict[str, Any] | None = None
        universe_decision: UniverseDecision | None = None
        if parse_result.accepted:
            candidate_market = parse_result.to_market()
            if existing_market is not None:
                candidate_market = candidate_market.with_fee_schedule(
                    fees_enabled=(
                        candidate_market.fees_enabled
                        if candidate_market.fees_enabled is not None
                        else existing_market.fees_enabled
                    ),
                    maker_base_fee_bps=(
                        candidate_market.maker_base_fee_bps
                        if candidate_market.maker_base_fee_bps is not None
                        else existing_market.maker_base_fee_bps
                    ),
                    taker_base_fee_bps=(
                        candidate_market.taker_base_fee_bps
                        if candidate_market.taker_base_fee_bps is not None
                        else existing_market.taker_base_fee_bps
                    ),
                )
                if (
                    existing_market.fee_rate_bps is not None
                    and candidate_market.fee_rate_bps is None
                    and candidate_market.taker_base_fee_bps is None
                ):
                    candidate_market = candidate_market.with_fee_rate(
                        existing_market.fee_rate_bps,
                        fee_rate_updated_at=existing_market.fee_rate_updated_at,
                    )

            universe_decision = self._extension_hooks.select_market(candidate_market)
            if universe_decision.selected:
                market = candidate_market
                tracked_market = market
                if self._registry is not None:
                    self._registry.upsert(market)
                if self._market_tracker is not None:
                    self._market_tracker.track_market(market)
                    if hasattr(self._market_tracker, "build_subscription_request"):
                        subscription_request = self._market_tracker.build_subscription_request(
                            market.token_ids
                        )
            elif existing_market is not None:
                if self._should_retain_filtered_market(existing_market, account_snapshot):
                    tracked_market = self._build_retained_filtered_market(
                        candidate_market,
                        existing_market=existing_market,
                        reason=universe_decision.reason,
                    )
                    tracking_retained = True
                    if self._registry is not None:
                        self._registry.upsert(tracked_market)
                    if self._market_tracker is not None:
                        self._market_tracker.track_market(tracked_market)
                else:
                    self._remove_market_tracking(existing_market)

        discovery_kind = (
            DomainEventType.MARKET_UPDATED.value
            if market is not None and existing_market is not None
            else (
                DomainEventType.MARKET_DISCOVERED.value
                if market is not None
                else DomainEventType.MARKET_FILTERED_OUT.value
            )
        )
        event = self._build_event(
            parse_result,
            trace_id=trace_id,
            source=source,
            discovered_at=discovered_at,
            discovery_kind=discovery_kind,
            raw_market=raw_market,
            market=market,
            tracked_market=tracked_market,
            tracking_retained=tracking_retained,
            universe_decision=universe_decision,
        )
        return MarketDiscoveryOutcome(
            trace_id=trace_id,
            source=source,
            parse_result=parse_result,
            event=event,
            market=market,
            discovery_kind=discovery_kind,
            subscription_request=subscription_request,
            raw_market=raw_market,
            existing_market=existing_market,
            tracked_market=tracked_market,
            tracking_retained=tracking_retained,
            tracking_removed=(
                parse_result.accepted
                and market is None
                and existing_market is not None
                and not tracking_retained
            ),
            universe_decision=universe_decision,
        )

    def _lookup_existing_market(
        self,
        parse_result: MarketParseResult,
    ) -> Market | None:
        if self._registry is None or not parse_result.accepted:
            return None
        if parse_result.condition_id is not None:
            market = self._registry.get_by_condition_id(parse_result.condition_id)
            if market is not None:
                return market
        if parse_result.market_slug is not None:
            return self._registry.get_by_slug(parse_result.market_slug)
        return None

    def _build_event(
        self,
        parse_result: MarketParseResult,
        *,
        trace_id: str,
        source: str,
        discovered_at: datetime,
        discovery_kind: str,
        raw_market: Mapping[str, Any],
        market: Market | None,
        tracked_market: Market | None,
        tracking_retained: bool,
        universe_decision: UniverseDecision | None,
    ) -> DomainEvent:
        event_type = (
            DomainEventType.MARKET_UPDATED
            if market is not None and discovery_kind == DomainEventType.MARKET_UPDATED.value
            else (
                DomainEventType.MARKET_DISCOVERED
                if market is not None
                else DomainEventType.MARKET_FILTERED_OUT
            )
        )
        event_slug = market.event_slug if market is not None else parse_result.event_slug
        payload = {
            "source": source,
            "discovery_kind": discovery_kind,
            "parse_status": parse_result.status.value,
            "parse_reason": parse_result.reject_reason.value if parse_result.reject_reason
            else None,
            "parse_detail": parse_result.reject_detail,
            "matched_fields": parse_result.matched_fields,
            "matched_keywords": parse_result.matched_keywords,
            "accepted": market is not None,
            "extension_selected": universe_decision.selected if universe_decision is not None else None,
            "extension_reason": universe_decision.reason if universe_decision is not None else None,
            "market": _serialize_market(market),
            "tracked_market": _serialize_market(tracked_market),
            "tracking_retained": tracking_retained,
            "raw_market": raw_market,
            "discovered_at": discovered_at.isoformat(),
        }
        return DomainEvent(
            trace_id=trace_id,
            event_type=event_type,
            event_id=uuid4().hex,
            market_slug=parse_result.market_slug,
            event_slug=event_slug,
            condition_id=parse_result.condition_id,
            reason=self._event_reason(parse_result, universe_decision),
            created_at=discovered_at,
            payload=payload,
        )

    @staticmethod
    def _event_reason(
        parse_result: MarketParseResult,
        universe_decision: UniverseDecision | None,
    ) -> str:
        if universe_decision is not None and not universe_decision.selected:
            return universe_decision.reason
        if parse_result.reject_reason is not None:
            return parse_result.reject_reason.value
        return ""

    def _current_account_snapshot(self) -> AccountSnapshot | None:
        if self._account_snapshot_provider is None:
            return None
        return self._account_snapshot_provider()

    def _should_retain_filtered_market(
        self,
        market: Market,
        account_snapshot: AccountSnapshot | None,
    ) -> bool:
        return self._extension_hooks.should_keep_tracking(market, account_snapshot)

    def _build_retained_filtered_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return self._extension_hooks.build_filtered_tracking_market(
            candidate_market,
            existing_market=existing_market,
            reason=reason,
        )

    def _remove_market_tracking(self, market: Market) -> None:
        if self._registry is not None:
            self._registry.remove_market(market.condition_id)
        if self._market_tracker is not None and hasattr(self._market_tracker, "untrack_market"):
            self._market_tracker.untrack_market(market.token_ids)


@dataclass(frozen=True, slots=True)
class MarketDiscoveryOutcome:
    trace_id: str
    source: str
    parse_result: MarketParseResult
    event: DomainEvent
    market: Market | None
    discovery_kind: str
    subscription_request: dict[str, Any] | None
    raw_market: Mapping[str, Any]
    existing_market: Market | None = None
    tracked_market: Market | None = None
    tracking_retained: bool = False
    tracking_removed: bool = False
    universe_decision: UniverseDecision | None = None

    @property
    def accepted(self) -> bool:
        return self.market is not None

    @property
    def should_publish_event(self) -> bool:
        return self.accepted or self.tracking_retained or self.tracking_removed


def _serialize_market(market: Market | None) -> dict[str, Any] | None:
    if market is None:
        return None
    return {
        "condition_id": market.condition_id,
        "market_slug": market.market_slug,
        "token_ids": list(market.token_ids),
        "outcomes": [
            {
                "token_id": outcome.token_id,
                "outcome": outcome.outcome,
            }
            for outcome in market.outcomes
        ],
        "event_id": market.event_id,
        "event_title": market.event_title,
        "event_slug": market.event_slug,
        "tick_size": str(market.tick_size),
        "min_order_size": str(market.min_order_size),
        "neg_risk": market.neg_risk,
        "fees": {
            "enabled": market.fees_enabled,
            "maker_base_fee_bps": market.maker_base_fee_bps,
            "taker_base_fee_bps": market.taker_base_fee_bps,
            "fee_rate_bps": market.fee_rate_bps,
            "fee_rate_updated_at": (
                None
                if market.fee_rate_updated_at is None
                else market.fee_rate_updated_at.isoformat()
            ),
        },
        "category": market.category,
        "tags": market.tags,
        "matched_keywords": market.matched_keywords,
        "trading_status": market.trading_status.value,
        "reject_reason": market.reject_reason,
    }
