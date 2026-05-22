from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from json import loads
from typing import Any, Mapping

from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus

_FEE_RATE_DENOMINATOR = Decimal("1000")


class MarketParseStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class MarketParseRejectReason(StrEnum):
    MISSING_TRADING_CONDITIONS = "missing_trading_conditions"
    FIELD_PARSE_FAILED = "field_parse_failed"


@dataclass(frozen=True, slots=True)
class MatchSignal:
    field_name: str
    keyword: str


@dataclass(frozen=True, slots=True)
class MarketParseResult:
    status: MarketParseStatus
    condition_id: str | None
    outcomes: tuple[MarketOutcome, ...]
    tick_size: Decimal | None
    min_order_size: Decimal | None
    neg_risk: bool
    fees_enabled: bool | None
    maker_base_fee_bps: int | None
    taker_base_fee_bps: int | None
    category: str | None
    tags: tuple[str, ...]
    sports_market_type: str | None
    market_name: str | None
    market_question: str | None
    market_slug: str | None
    event_title: str | None
    event_slug: str | None
    event_id: str | None
    icon_url: str | None
    end_date: datetime | None
    game_start_time: datetime | None
    matched_fields: tuple[str, ...] = field(default_factory=tuple)
    matched_keywords: tuple[str, ...] = field(default_factory=tuple)
    reject_reason: MarketParseRejectReason | None = None
    reject_detail: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status is MarketParseStatus.ACCEPTED

    @property
    def event_type(self) -> DomainEventType:
        return (
            DomainEventType.MARKET_FILTERED_IN
            if self.accepted
            else DomainEventType.MARKET_FILTERED_OUT
        )

    def to_event(
        self,
        *,
        trace_id: str,
        event_id: str,
        created_at: datetime | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> DomainEvent:
        payload_data: dict[str, Any] = {
            "accepted": self.accepted,
            "parse_status": self.status.value,
            "condition_id": self.condition_id,
            "outcomes": tuple(
                {
                    "token_id": outcome.token_id,
                    "outcome": outcome.outcome,
                }
                for outcome in self.outcomes
            ),
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
            "neg_risk": self.neg_risk,
            "fees_enabled": self.fees_enabled,
            "maker_base_fee_bps": self.maker_base_fee_bps,
            "taker_base_fee_bps": self.taker_base_fee_bps,
            "category": self.category,
            "tags": self.tags,
            "sports_market_type": self.sports_market_type,
            "market_name": self.market_name,
            "market_question": self.market_question,
            "market_slug": self.market_slug,
            "event_title": self.event_title,
            "event_id": self.event_id,
            "icon_url": self.icon_url,
            "end_date": self.end_date,
            "game_start_time": self.game_start_time,
            "matched_fields": self.matched_fields,
            "matched_keywords": self.matched_keywords,
            "parse_reason": self.reject_reason.value if self.reject_reason else None,
            "parse_detail": self.reject_detail,
        }
        if payload:
            payload_data.update(payload)
        return DomainEvent(
            trace_id=trace_id,
            event_type=self.event_type,
            event_id=event_id,
            market_slug=self.market_slug,
            event_slug=self.event_slug,
            condition_id=self.condition_id,
            reason=self.reject_reason.value if self.reject_reason else "",
            created_at=created_at or datetime.now(timezone.utc),
            payload=payload_data,
        )

    def to_market(
        self,
        *,
        trading_status: TradingStatus = TradingStatus.ELIGIBLE,
    ) -> Market:
        if not self.accepted:
            raise ValueError("rejected parse result cannot be converted to Market")
        if (
            self.condition_id is None
            or self.market_slug is None
            or not self.outcomes
            or self.tick_size is None
            or self.min_order_size is None
        ):
            raise ValueError("accepted parse result is missing required market fields")
        return Market(
            condition_id=self.condition_id,
            market_slug=self.market_slug,
            outcomes=self.outcomes,
            market_name=self.market_name,
            market_question=self.market_question,
            event_id=self.event_id,
            event_title=self.event_title,
            event_slug=self.event_slug,
            icon_url=self.icon_url,
            end_date=self.end_date,
            game_start_time=self.game_start_time,
            tick_size=self.tick_size,
            min_order_size=self.min_order_size,
            neg_risk=self.neg_risk,
            fees_enabled=self.fees_enabled,
            maker_base_fee_bps=self.maker_base_fee_bps,
            taker_base_fee_bps=self.taker_base_fee_bps,
            fee_rate_bps=self.taker_base_fee_bps,
            category=self.category,
            tags=self.tags,
            sports_market_type=self.sports_market_type,
            matched_keywords=self.matched_keywords,
            trading_status=trading_status,
        )


class MarketPayloadParser:
    """Parse raw market payloads into generic market candidates."""

    def parse(self, raw_market: Mapping[str, Any]) -> MarketParseResult:
        parsed = self._parse_market_fields(raw_market)
        match_signals: list[MatchSignal] = []

        if parsed["parse_error"] is not None:
            return self._reject(
                MarketParseRejectReason.FIELD_PARSE_FAILED,
                parsed,
                match_signals,
                parsed["parse_error"],
            )

        if (
            parsed["condition_id"] is None
            or parsed["market_slug"] is None
            or not parsed["outcomes"]
        ):
            return self._reject(
                MarketParseRejectReason.MISSING_TRADING_CONDITIONS,
                parsed,
                match_signals,
                "missing condition_id / market_slug / outcomes",
            )

        if parsed["tick_size"] is None or parsed["min_order_size"] is None:
            return self._reject(
                MarketParseRejectReason.MISSING_TRADING_CONDITIONS,
                parsed,
                match_signals,
                "missing tick_size / min_order_size",
            )

        return MarketParseResult(
            status=MarketParseStatus.ACCEPTED,
            condition_id=parsed["condition_id"],
            outcomes=parsed["outcomes"],
            tick_size=parsed["tick_size"],
            min_order_size=parsed["min_order_size"],
            neg_risk=parsed["neg_risk"],
            fees_enabled=parsed["fees_enabled"],
            maker_base_fee_bps=parsed["maker_base_fee_bps"],
            taker_base_fee_bps=parsed["taker_base_fee_bps"],
            category=parsed["category"],
            tags=parsed["tags"],
            sports_market_type=parsed.get("sports_market_type"),
            market_name=parsed["market_name"],
            market_question=parsed["market_question"],
            market_slug=parsed["market_slug"],
            event_title=parsed["event_title"],
            event_slug=parsed["event_slug"],
            event_id=parsed["event_id"],
            icon_url=parsed["icon_url"],
            end_date=parsed["end_date"],
            game_start_time=parsed["game_start_time"],
            matched_fields=tuple(signal.field_name for signal in match_signals),
            matched_keywords=tuple(signal.keyword for signal in match_signals),
        )

    def _reject(
        self,
        reason: MarketParseRejectReason,
        parsed: Mapping[str, Any],
        match_signals: list[MatchSignal],
        reject_detail: str,
        *,
        matched_fields: tuple[str, ...] | None = None,
        matched_keywords: tuple[str, ...] | None = None,
    ) -> MarketParseResult:
        fields = matched_fields or tuple(signal.field_name for signal in match_signals)
        keywords = matched_keywords or tuple(signal.keyword for signal in match_signals)
        return MarketParseResult(
            status=MarketParseStatus.REJECTED,
            condition_id=parsed["condition_id"],
            outcomes=parsed["outcomes"],
            tick_size=parsed["tick_size"],
            min_order_size=parsed["min_order_size"],
            neg_risk=parsed["neg_risk"],
            fees_enabled=parsed["fees_enabled"],
            maker_base_fee_bps=parsed["maker_base_fee_bps"],
            taker_base_fee_bps=parsed["taker_base_fee_bps"],
            category=parsed["category"],
            tags=parsed["tags"],
            sports_market_type=parsed.get("sports_market_type"),
            market_name=parsed["market_name"],
            market_question=parsed["market_question"],
            market_slug=parsed["market_slug"],
            event_title=parsed["event_title"],
            event_slug=parsed["event_slug"],
            event_id=parsed["event_id"],
            icon_url=parsed["icon_url"],
            end_date=parsed["end_date"],
            game_start_time=parsed["game_start_time"],
            matched_fields=fields,
            matched_keywords=keywords,
            reject_reason=reason,
            reject_detail=reject_detail,
        )

    def _parse_market_fields(self, raw_market: Mapping[str, Any]) -> dict[str, Any]:
        try:
            event = self._first_event(raw_market)
            token_ids = self._parse_token_ids(self._first_value(raw_market, "clobTokenIds"))
            outcome_names = self._parse_outcome_names(self._first_value(raw_market, "outcomes"))
            condition_id = self._parse_text(self._first_value(raw_market, "conditionId"))
            outcomes = self._build_outcomes(token_ids, outcome_names)
            tick_size = self._parse_decimal(
                self._first_value(raw_market, "orderPriceMinTickSize", "tickSize", "tick")
            )
            # orderMinSize 是 Polymarket 市场字段，语义是最小订单 size；
            # 不要和本系统 MAX_ORDER_USDC 这类单笔资金配置混用。
            min_order_size = self._parse_decimal(
                self._first_value(
                    raw_market,
                    "orderMinSize",
                    "minOrderSize",
                    "min_size",
                    "minSize",
                )
            )
            neg_risk = self._parse_bool(self._first_value(raw_market, "negRisk"))
            fee_schedule = self._as_mapping(self._first_value(raw_market, "feeSchedule"))
            fees_enabled = self._parse_nullable_bool(
                self._first_value(raw_market, "feesEnabled")
            )
            if fees_enabled is None and fee_schedule is not None:
                fees_enabled = self._parse_nullable_bool(
                    self._first_value(fee_schedule, "enabled", "feesEnabled")
                )
            maker_base_fee_bps = self._parse_int(
                self._first_value(
                    raw_market,
                    "makerBaseFee",
                )
            )
            taker_base_fee_bps = (
                self._parse_fee_rate_units(
                    self._first_value(
                        fee_schedule,
                        "rate",
                        "base_fee",
                        "baseFee",
                    )
                )
                if fee_schedule is not None
                else None
            )
            if taker_base_fee_bps is None:
                taker_base_fee_bps = self._parse_int(
                    self._first_value(
                        raw_market,
                        "takerBaseFee",
                    )
                )
            category = self._parse_text(self._first_value(raw_market, "category"))
            if category is None and event is not None:
                category = self._parse_text(self._first_value(event, "category"))
            tags = self._parse_tags(self._first_value(raw_market, "tags"))
            if not tags and event is not None:
                tags = self._parse_tags(self._first_value(event, "tags"))
            sports_market_type = self._parse_text(
                self._first_value(raw_market, "sportsMarketType", "sports_market_type")
            )
            if sports_market_type is None and event is not None:
                sports_market_type = self._parse_text(
                    self._first_value(event, "sportsMarketType", "sports_market_type")
                )
            market_name = self._parse_text(self._first_value(raw_market, "name", "marketName"))
            market_question = self._parse_text(
                self._first_value(
                    raw_market,
                    "question",
                    "prompt",
                )
            )
            market_slug = self._parse_text(self._first_value(raw_market, "slug"))
            event_title = self._parse_text(
                self._first_value(
                    raw_market,
                    "eventTitle",
                )
            )
            if event_title is None and event is not None:
                event_title = self._parse_text(self._first_value(event, "title", "name"))
            event_slug = self._parse_text(self._first_value(raw_market, "eventSlug"))
            if event_slug is None and event is not None:
                event_slug = self._parse_text(self._first_value(event, "slug"))
            event_id = self._parse_text(self._first_value(raw_market, "eventId"))
            if event_id is None and event is not None:
                event_id = self._parse_text(self._first_value(event, "id"))
            icon_url = self._parse_text(self._first_value(raw_market, "icon"))
            if icon_url is None and event is not None:
                icon_url = self._parse_text(self._first_value(event, "icon"))
            end_date = self._parse_datetime(self._first_value(raw_market, "endDate", "end_date"))
            if end_date is None and event is not None:
                end_date = self._parse_datetime(self._first_value(event, "endDate", "end_date"))
            game_start_time = self._parse_datetime(
                self._first_value(
                    raw_market,
                    "gameStartTime",
                    "game_start_time",
                    "gameStart",
                )
            )
            if game_start_time is None and event is not None:
                game_start_time = self._parse_datetime(
                    self._first_value(
                        event,
                        "gameStartTime",
                        "game_start_time",
                        "gameStart",
                    )
                )
        except (TypeError, ValueError) as exc:
            return {
                "condition_id": None,
                "outcomes": tuple(),
                "tick_size": None,
                "min_order_size": None,
                "neg_risk": False,
                "fees_enabled": None,
                "maker_base_fee_bps": None,
                "taker_base_fee_bps": None,
                "category": None,
                "tags": tuple(),
                "market_name": None,
                "market_question": None,
                "market_slug": None,
                "event_title": None,
                "event_slug": None,
                "event_id": None,
                "icon_url": None,
                "end_date": None,
                "game_start_time": None,
                "parse_error": f"{type(exc).__name__}: {exc}",
            }

        return {
            "condition_id": condition_id,
            "outcomes": outcomes,
            "tick_size": tick_size,
            "min_order_size": min_order_size,
            "neg_risk": neg_risk,
            "fees_enabled": fees_enabled,
            "maker_base_fee_bps": maker_base_fee_bps,
            "taker_base_fee_bps": taker_base_fee_bps,
            "category": category,
            "tags": tags,
            "sports_market_type": sports_market_type,
            "market_name": market_name,
            "market_question": market_question,
            "market_slug": market_slug,
            "event_title": event_title,
            "event_slug": event_slug,
            "event_id": event_id,
            "icon_url": icon_url,
            "end_date": end_date,
            "game_start_time": game_start_time,
            "parse_error": None,
        }

    @staticmethod
    def _collect_text(raw_market: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
        values: list[str] = []
        for key in keys:
            value = raw_market.get(key)
            if value is None:
                continue
            if isinstance(value, Mapping):
                values.extend(str(item) for item in value.values() if item is not None)
                continue
            if isinstance(value, (list, tuple, set)):
                values.extend(str(item) for item in value if item is not None)
                continue
            values.append(str(value))
        return tuple(values)

    @staticmethod
    def _first_value(raw_market: Mapping[str, Any], *keys: str) -> Any | None:
        for key in keys:
            if key in raw_market and raw_market[key] is not None:
                return raw_market[key]
        return None

    @staticmethod
    def _parse_decimal(value: Any | None) -> Decimal | None:
        if value is None or value == "":
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @staticmethod
    def _parse_bool(value: Any | None) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        normalized = str(value).strip().lower()
        return normalized in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _parse_datetime(value: Any | None) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _parse_nullable_bool(value: Any | None) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if not normalized:
            return None
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
        return None

    @staticmethod
    def _parse_int(value: Any | None) -> int | None:
        if value is None or value == "":
            return None
        return int(Decimal(str(value)))

    @staticmethod
    def _parse_fee_rate_units(value: Any | None) -> int | None:
        if isinstance(value, bool):
            return None
        numeric = MarketPayloadParser._parse_decimal(value)
        if numeric is None or numeric < Decimal("0"):
            return None
        if numeric < Decimal("1"):
            return int((numeric * _FEE_RATE_DENOMINATOR).to_integral_value())
        if numeric == numeric.to_integral_value():
            return int(numeric)
        return int((numeric * _FEE_RATE_DENOMINATOR).to_integral_value())

    @staticmethod
    def _parse_text(value: Any | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _as_mapping(value: Any | None) -> Mapping[str, Any] | None:
        if isinstance(value, Mapping):
            return value
        return None

    @staticmethod
    def _first_event(raw_market: Mapping[str, Any]) -> Mapping[str, Any] | None:
        events = raw_market.get("events")
        if isinstance(events, list):
            for item in events:
                if isinstance(item, Mapping):
                    return item
        return None

    @staticmethod
    def _parse_tags(value: Any | None) -> tuple[str, ...]:
        if value is None:
            return tuple()
        if isinstance(value, str):
            return tuple(tag.strip() for tag in value.split(",") if tag.strip())
        if isinstance(value, Mapping):
            mapping_tags: list[str] = []
            for key in ("label", "slug", "name"):
                text = MarketPayloadParser._parse_text(value.get(key))
                if text is not None:
                    mapping_tags.append(text)
            return tuple(mapping_tags)
        if isinstance(value, (list, tuple, set)):
            nested_tags: list[str] = []
            for tag in value:
                nested_tags.extend(MarketPayloadParser._parse_tags(tag))
            return tuple(nested_tags)
        return (str(value).strip(),)

    @staticmethod
    def _parse_token_ids(value: Any | None) -> tuple[str, ...]:
        if value is None:
            return tuple()
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return tuple()
            try:
                parsed = loads(text)
            except ValueError:
                return (text,)
            return MarketPayloadParser._parse_token_ids(parsed)
        if isinstance(value, (list, tuple, set)):
            token_ids: list[str] = []
            for item in value:
                parsed_text = MarketPayloadParser._parse_text(item)
                if parsed_text is not None:
                    token_ids.append(parsed_text)
            return tuple(token_ids)
        parsed_text = MarketPayloadParser._parse_text(value)
        return tuple() if parsed_text is None else (parsed_text,)

    @staticmethod
    def _parse_outcome_names(value: Any | None) -> tuple[str, ...]:
        if value is None:
            return tuple()
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return tuple()
            try:
                parsed = loads(text)
            except ValueError:
                return (text,)
            return MarketPayloadParser._parse_outcome_names(parsed)
        if isinstance(value, (list, tuple, set)):
            outcome_names: list[str] = []
            for item in value:
                parsed_text = MarketPayloadParser._parse_text(item)
                if parsed_text is not None:
                    outcome_names.append(parsed_text)
            return tuple(outcome_names)
        parsed_text = MarketPayloadParser._parse_text(value)
        return tuple() if parsed_text is None else (parsed_text,)

    @staticmethod
    def _build_outcomes(
        token_ids: tuple[str, ...],
        outcome_names: tuple[str, ...],
    ) -> tuple[MarketOutcome, ...]:
        if not token_ids:
            return tuple()
        resolved_names = list(outcome_names)
        if len(resolved_names) < len(token_ids):
            if not resolved_names and len(token_ids) == 2:
                resolved_names = ["YES", "NO"]
            while len(resolved_names) < len(token_ids):
                resolved_names.append(f"OUTCOME_{len(resolved_names)}")
        return tuple(
            MarketOutcome(token_id=token_id, outcome=resolved_names[index])
            for index, token_id in enumerate(token_ids)
        )

    @staticmethod
    def _matched_signals(
        field_name: str,
        text: str,
        keywords: tuple[str, ...],
    ) -> tuple[MatchSignal, ...]:
        signals: list[MatchSignal] = []
        for keyword in keywords:
            if keyword in text:
                signals.append(MatchSignal(field_name=field_name, keyword=keyword))
        return tuple(signals)
