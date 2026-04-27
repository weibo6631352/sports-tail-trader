from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from polymarket_trader.domain.events import sanitize_raw_response


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class RawMarketEvent:
    source: str
    payload: Mapping[str, Any]
    trace_id: str = ""
    discovered_at: datetime = field(default_factory=_utc_now)
    condition_id: str | None = None
    market_slug: str | None = None
    dedupe_key: str | None = None
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", self.source.strip())

        trace_id = self.trace_id.strip() if self.trace_id else ""
        if not trace_id:
            trace_id = uuid4().hex
        object.__setattr__(self, "trace_id", trace_id)

        discovered_at = getattr(self, "discovered_at", None)
        if not isinstance(discovered_at, datetime):
            discovered_at = _utc_now()
        if discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=timezone.utc)
        else:
            discovered_at = discovered_at.astimezone(timezone.utc)
        object.__setattr__(self, "discovered_at", discovered_at)

        condition_id = self.condition_id or _first_text(
            self.payload,
            "condition_id",
            "conditionId",
            "condition",
        )
        market_slug = self.market_slug or _first_text(self.payload, "market_slug", "marketSlug", "slug")
        object.__setattr__(self, "condition_id", condition_id)
        object.__setattr__(self, "market_slug", market_slug)

        dedupe_key = self.dedupe_key or condition_id or market_slug
        object.__setattr__(self, "dedupe_key", dedupe_key)

        summary = self.summary.strip() if self.summary else ""
        if not summary:
            summary = sanitize_raw_response(self.payload) or ""
        object.__setattr__(self, "summary", summary)

    @property
    def dedupe_identity(self) -> str | None:
        return self.dedupe_key or self.condition_id or self.market_slug

    @property
    def payload_preview(self) -> str:
        return self.summary


def _first_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
