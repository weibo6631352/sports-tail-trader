"""Gamma API（events / markets / public profile）schema 层。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from polymarket_trader.domain.discovery import RawMarketEvent
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.infra.polymarket.base_client import _summary

from ._helpers import (
    _coerce_bool,
    _coerce_datetime,
    _coerce_decimal,
    _coerce_fee_rate_units,
    _coerce_int,
    _first_text,
    _first_value,
    _iter_mappings,
    _market_outcomes,
    _maybe_mapping,
    _string_tuple,
    _token_id_tuple,
    _unwrap_mapping,
)


@dataclass(frozen=True, slots=True)
class GammaPublicProfileDTO:
    raw: Mapping[str, Any]
    proxy_wallet: str | None = None
    profile_image: str | None = None
    display_username_public: bool | None = None
    name: str | None = None
    pseudonym: str | None = None
    x_username: str | None = None
    verified_badge: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "proxy_wallet", self.proxy_wallet or _first_text(self.raw, "proxyWallet"))
        object.__setattr__(self, "profile_image", self.profile_image or _first_text(self.raw, "profileImage"))
        object.__setattr__(
            self,
            "display_username_public",
            self.display_username_public
            if self.display_username_public is not None
            else _coerce_bool(_first_value(self.raw, "displayUsernamePublic")),
        )
        object.__setattr__(self, "name", self.name or _first_text(self.raw, "name"))
        object.__setattr__(self, "pseudonym", self.pseudonym or _first_text(self.raw, "pseudonym"))
        object.__setattr__(self, "x_username", self.x_username or _first_text(self.raw, "xUsername"))
        object.__setattr__(
            self,
            "verified_badge",
            self.verified_badge
            if self.verified_badge is not None
            else _coerce_bool(_first_value(self.raw, "verifiedBadge")),
        )


@dataclass(frozen=True, slots=True)
class GammaMarketDTO:
    raw: Mapping[str, Any]
    condition_id: str | None = None
    market_slug: str | None = None
    question: str | None = None
    title: str | None = None
    event_id: str | None = None
    event_title: str | None = None
    event_slug: str | None = None
    icon_url: str | None = None
    end_date: datetime | None = None
    game_start_time: datetime | None = None
    outcomes: tuple[MarketOutcome, ...] = field(default_factory=tuple)
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    neg_risk: bool = False
    fees_enabled: bool | None = None
    maker_base_fee_bps: int | None = None
    taker_base_fee_bps: int | None = None
    category: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    active: bool | None = None
    closed: bool | None = None
    archived: bool | None = None
    clob_enabled: bool | None = None
    raw_summary: str = ""

    def __post_init__(self) -> None:
        event = next(iter(_iter_mappings(self.raw, "events")), None)
        fee_schedule = _maybe_mapping(_first_value(self.raw, "fee_schedule", "feeSchedule"))
        token_ids = _token_id_tuple(_first_value(self.raw, "clobTokenIds"))
        outcome_names = _string_tuple(_first_value(self.raw, "outcomes"))
        raw_fees_enabled = _first_value(self.raw, "fees_enabled", "feesEnabled")
        fee_schedule_rate_bps = (
            _coerce_fee_rate_units(_first_value(fee_schedule, "rate", "base_fee", "baseFee"))
            if fee_schedule is not None
            else None
        )
        raw_taker_base_fee = _first_value(
            self.raw,
            "taker_base_fee_bps",
            "takerBaseFee",
            "taker_base_fee",
        )
        raw_tags = _first_value(self.raw, "tags")
        if raw_tags is None and event is not None:
            raw_tags = _first_value(event, "tags")
        object.__setattr__(self, "condition_id", self.condition_id or _first_text(self.raw, "condition_id", "conditionId", "condition"))
        object.__setattr__(self, "market_slug", self.market_slug or _first_text(self.raw, "market_slug", "marketSlug", "slug"))
        object.__setattr__(self, "question", self.question or _first_text(self.raw, "question", "prompt", "market_question"))
        object.__setattr__(self, "title", self.title or _first_text(self.raw, "title", "name"))
        object.__setattr__(self, "event_id", self.event_id or _first_text(self.raw, "event_id", "eventId", "id") or _first_text(event or {}, "id"))
        object.__setattr__(self, "event_title", self.event_title or _first_text(self.raw, "event_title", "eventTitle") or _first_text(event or {}, "title", "name"))
        object.__setattr__(self, "event_slug", self.event_slug or _first_text(self.raw, "event_slug", "eventSlug") or _first_text(event or {}, "slug"))
        object.__setattr__(self, "icon_url", self.icon_url or _first_text(self.raw, "icon") or _first_text(event or {}, "icon"))
        object.__setattr__(
            self,
            "end_date",
            self.end_date
            if self.end_date is not None
            else _coerce_datetime(
                _first_value(self.raw, "endDate", "end_date")
                or _first_value(event or {}, "endDate", "end_date")
            ),
        )
        object.__setattr__(
            self,
            "game_start_time",
            self.game_start_time
            if self.game_start_time is not None
            else _coerce_datetime(
                _first_value(
                    self.raw,
                    "gameStartTime",
                    "game_start_time",
                    "gameStart",
                )
                or _first_value(
                    event or {},
                    "gameStartTime",
                    "game_start_time",
                    "gameStart",
                )
            ),
        )
        object.__setattr__(
            self,
            "outcomes",
            self.outcomes or _market_outcomes(token_ids, outcome_names),
        )
        object.__setattr__(self, "tick_size", self.tick_size if self.tick_size is not None else _coerce_decimal(_first_value(self.raw, "orderPriceMinTickSize", "tick_size", "tickSize")))
        object.__setattr__(self, "min_order_size", self.min_order_size if self.min_order_size is not None else _coerce_decimal(_first_value(self.raw, "orderMinSize", "min_order_size", "minOrderSize")))
        object.__setattr__(self, "neg_risk", self.neg_risk or bool(_coerce_bool(_first_value(self.raw, "neg_risk", "negRisk"))))
        object.__setattr__(
            self,
            "fees_enabled",
            self.fees_enabled
            if self.fees_enabled is not None
            else (
                _coerce_bool(raw_fees_enabled)
                if raw_fees_enabled is not None
                else (
                    None
                    if fee_schedule is None
                    else _coerce_bool(_first_value(fee_schedule, "enabled", "feesEnabled"))
                )
            ),
        )
        object.__setattr__(
            self,
            "maker_base_fee_bps",
            self.maker_base_fee_bps
            if self.maker_base_fee_bps is not None
            else _coerce_int(_first_value(self.raw, "maker_base_fee_bps", "makerBaseFee", "maker_base_fee")),
        )
        object.__setattr__(
            self,
            "taker_base_fee_bps",
            self.taker_base_fee_bps
            if self.taker_base_fee_bps is not None
            else (
                fee_schedule_rate_bps
                if fee_schedule_rate_bps is not None
                else _coerce_int(raw_taker_base_fee)
            ),
        )
        object.__setattr__(self, "category", self.category or _first_text(self.raw, "category", "cat") or _first_text(event or {}, "category"))
        object.__setattr__(self, "tags", self.tags or _string_tuple(raw_tags))
        object.__setattr__(self, "active", self.active if self.active is not None else _coerce_bool(_first_value(self.raw, "active", "is_active")))
        object.__setattr__(self, "closed", self.closed if self.closed is not None else _coerce_bool(_first_value(self.raw, "closed", "is_closed")))
        object.__setattr__(self, "archived", self.archived if self.archived is not None else _coerce_bool(_first_value(self.raw, "archived", "is_archived")))
        object.__setattr__(self, "clob_enabled", self.clob_enabled if self.clob_enabled is not None else _coerce_bool(_first_value(self.raw, "clob_enabled", "enableOrderBook", "clobEnabled")))
        summary = self.raw_summary.strip() if self.raw_summary else ""
        if not summary:
            summary = _summary(self.raw) or ""
        object.__setattr__(self, "raw_summary", summary)

    def to_market(self) -> Market:
        if self.condition_id is None or self.market_slug is None or not self.outcomes:
            raise ValueError("gamma market payload missing required market identifiers")
        tick_size = self.tick_size or Decimal("0.01")
        min_order_size = self.min_order_size or Decimal("1")
        status = TradingStatus.ELIGIBLE
        if self.closed:
            status = TradingStatus.CLOSED
        elif self.archived:
            status = TradingStatus.PAUSED
        return Market(
            condition_id=self.condition_id,
            market_slug=self.market_slug,
            outcomes=self.outcomes,
            market_name=self.title,
            market_question=self.question,
            event_id=self.event_id,
            event_title=self.event_title,
            event_slug=self.event_slug,
            icon_url=self.icon_url,
            end_date=self.end_date,
            game_start_time=self.game_start_time,
            tick_size=tick_size,
            min_order_size=min_order_size,
            neg_risk=self.neg_risk,
            fees_enabled=self.fees_enabled,
            maker_base_fee_bps=self.maker_base_fee_bps,
            taker_base_fee_bps=self.taker_base_fee_bps,
            fee_rate_bps=self.taker_base_fee_bps,
            category=self.category,
            tags=self.tags,
            trading_status=status,
        )

    def to_raw_market_event(self, *, source: str, trace_id: str | None = None) -> RawMarketEvent:
        return RawMarketEvent(
            source=source,
            payload=self.raw,
            trace_id=trace_id or "",
            condition_id=self.condition_id,
            market_slug=self.market_slug,
            summary=self.raw_summary,
        )


@dataclass(frozen=True, slots=True)
class GammaEventDTO:
    raw: Mapping[str, Any]
    event_id: str | None = None
    event_slug: str | None = None
    event_title: str | None = None
    category: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    markets: tuple[GammaMarketDTO, ...] = field(default_factory=tuple)
    active: bool | None = None
    closed: bool | None = None
    raw_summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", self.event_id or _first_text(self.raw, "event_id", "eventId", "id"))
        object.__setattr__(self, "event_slug", self.event_slug or _first_text(self.raw, "event_slug", "eventSlug", "slug"))
        object.__setattr__(self, "event_title", self.event_title or _first_text(self.raw, "event_title", "title", "name"))
        object.__setattr__(self, "category", self.category or _first_text(self.raw, "category"))
        object.__setattr__(self, "tags", self.tags or _string_tuple(_first_value(self.raw, "tags")))
        object.__setattr__(self, "active", self.active if self.active is not None else _coerce_bool(_first_value(self.raw, "active", "is_active")))
        object.__setattr__(self, "closed", self.closed if self.closed is not None else _coerce_bool(_first_value(self.raw, "closed", "is_closed")))
        summary = self.raw_summary.strip() if self.raw_summary else ""
        if not summary:
            summary = _summary(self.raw) or ""
        object.__setattr__(self, "raw_summary", summary)

    def iter_markets(self) -> tuple[GammaMarketDTO, ...]:
        if self.markets:
            return self.markets
        markets = []
        for item in _iter_mappings(self.raw, "markets", "items", "results"):
            markets.append(normalize_gamma_market(item))
        return tuple(markets)

    def to_raw_market_events(self, *, source: str, trace_id: str | None = None) -> tuple[RawMarketEvent, ...]:
        markets = self.iter_markets()
        if not markets:
            return (
                RawMarketEvent(
                    source=source,
                    payload=self.raw,
                    trace_id=trace_id or "",
                    condition_id=_first_text(self.raw, "condition_id", "conditionId", "condition"),
                    market_slug=_first_text(self.raw, "market_slug", "marketSlug", "slug"),
                    summary=self.raw_summary,
                ),
            )
        raw_tags = _first_value(self.raw, "tags")
        raw_category = _first_value(self.raw, "category")
        raw_events: list[RawMarketEvent] = []
        for market in markets:
            payload = dict(market.raw)
            if _first_value(payload, "eventId") is None and self.event_id is not None:
                payload["eventId"] = self.event_id
            if _first_value(payload, "eventSlug") is None and self.event_slug is not None:
                payload["eventSlug"] = self.event_slug
            if _first_value(payload, "eventTitle") is None and self.event_title is not None:
                payload["eventTitle"] = self.event_title
            if _first_value(payload, "tags") is None and raw_tags is not None:
                payload["tags"] = raw_tags
            if _first_value(payload, "category") is None and raw_category is not None:
                payload["category"] = raw_category
            raw_events.append(
                RawMarketEvent(
                    source=source,
                    payload=payload,
                    trace_id=trace_id or "",
                    condition_id=market.condition_id,
                    market_slug=market.market_slug,
                    summary=market.raw_summary,
                )
            )
        return tuple(raw_events)


def normalize_gamma_market(payload: Mapping[str, Any]) -> GammaMarketDTO:
    return GammaMarketDTO(raw=_unwrap_mapping(payload))


def normalize_gamma_event(payload: Mapping[str, Any]) -> GammaEventDTO:
    normalized = _unwrap_mapping(payload)
    markets = tuple(normalize_gamma_market(item) for item in _iter_mappings(normalized, "markets", "items", "results"))
    return GammaEventDTO(raw=normalized, markets=markets)


def normalize_gamma_public_profile(payload: Mapping[str, Any]) -> GammaPublicProfileDTO:
    return GammaPublicProfileDTO(raw=_unwrap_mapping(payload))


def gamma_event_to_raw_market_events(
    payload: Mapping[str, Any],
    *,
    source: str,
    trace_id: str | None = None,
) -> tuple[RawMarketEvent, ...]:
    return normalize_gamma_event(payload).to_raw_market_events(source=source, trace_id=trace_id)
