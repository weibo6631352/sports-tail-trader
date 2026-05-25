from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Mapping

from polymarket_trader.domain.events import DomainEvent
from polymarket_trader.domain.market import Market
from polymarket_trader.serialization import jsonable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _identity(
    *,
    condition_id: str | None = None,
    market_slug: str | None = None,
    event_slug: str | None = None,
) -> str:
    if condition_id:
        return f"condition:{condition_id}"
    if market_slug:
        return f"market:{market_slug}"
    if event_slug:
        return f"event:{event_slug}"
    raise ValueError("condition_id, market_slug, or event_slug is required")


@dataclass(frozen=True, slots=True)
class EntryMetadataRecord:
    """入场判断前可补充的运行时 metadata 快照。

    ``metadata`` 仍然是策略可透传的自由 dict（admin 详情透传用）；framework 决策
    只读强类型 ``live_state_*`` 字段，避免再依赖策略私有 metadata key。
    """

    condition_id: str | None = None
    market_slug: str | None = None
    event_slug: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source: str = "manual"
    updated_at: datetime = field(default_factory=_utc_now)
    live_state_signal_allowed: bool | None = None
    live_state_signal_reason: str = ""
    live_state_phase: str = ""
    live_state_payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return _identity(
            condition_id=self.condition_id,
            market_slug=self.market_slug,
            event_slug=self.event_slug,
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "market_slug": self.market_slug,
            "event_slug": self.event_slug,
            "metadata": jsonable(self.metadata),
            "source": self.source,
            "updated_at": self.updated_at.isoformat(),
            "live_state_signal_allowed": self.live_state_signal_allowed,
            "live_state_signal_reason": self.live_state_signal_reason,
            "live_state_phase": self.live_state_phase,
            "live_state_payload": jsonable(self.live_state_payload),
        }


class MarketMetadataStore:
    """按 market/event 维度保存入场前可读的轻量 metadata。

    该 store 不理解具体策略字段，只负责无阻塞读写运行时事实。
    """

    def __init__(self) -> None:
        self._records: dict[str, EntryMetadataRecord] = {}
        self._aliases: dict[str, str] = {}
        self._lock = Lock()

    def upsert(
        self,
        *,
        metadata: Mapping[str, Any],
        condition_id: str | None = None,
        market_slug: str | None = None,
        event_slug: str | None = None,
        source: str = "manual",
        updated_at: datetime | None = None,
        live_state_signal_allowed: bool | None = None,
        live_state_signal_reason: str = "",
        live_state_phase: str = "",
        live_state_payload: Mapping[str, Any] | None = None,
    ) -> EntryMetadataRecord:
        record = EntryMetadataRecord(
            condition_id=condition_id,
            market_slug=market_slug,
            event_slug=event_slug,
            metadata=dict(metadata),
            source=source,
            updated_at=updated_at or _utc_now(),
            live_state_signal_allowed=live_state_signal_allowed,
            live_state_signal_reason=live_state_signal_reason,
            live_state_phase=live_state_phase,
            live_state_payload=dict(live_state_payload or {}),
        )
        aliases = _record_identity_keys(record)
        with self._lock:
            self._records[record.key] = record
            self._remove_aliases_for_primary(record.key)
            for alias in aliases:
                self._aliases[alias] = record.key
        return record

    def remove(
        self,
        *,
        condition_id: str | None = None,
        market_slug: str | None = None,
        event_slug: str | None = None,
    ) -> bool:
        key = _identity(condition_id=condition_id, market_slug=market_slug, event_slug=event_slug)
        with self._lock:
            primary_key = self._aliases.get(key, key)
            removed = self._records.pop(primary_key, None) is not None
            if removed:
                self._remove_aliases_for_primary(primary_key)
            return removed

    def records(self) -> tuple[EntryMetadataRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def metadata_for(
        self,
        *,
        condition_id: str | None = None,
        market_slug: str | None = None,
        event_slug: str | None = None,
    ) -> dict[str, Any]:
        record = self.find(
            condition_id=condition_id,
            market_slug=market_slug,
            event_slug=event_slug,
        )
        return {} if record is None else dict(record.metadata)

    def find(
        self,
        *,
        condition_id: str | None = None,
        market_slug: str | None = None,
        event_slug: str | None = None,
    ) -> EntryMetadataRecord | None:
        keys = []
        if condition_id:
            keys.append(_identity(condition_id=condition_id))
        if market_slug:
            keys.append(_identity(market_slug=market_slug))
        if event_slug:
            keys.append(_identity(event_slug=event_slug))
        with self._lock:
            for key in keys:
                record = self._records.get(self._aliases.get(key, key))
                if record is not None:
                    return record
        return None

    def _remove_aliases_for_primary(self, primary_key: str) -> None:
        for alias, target in tuple(self._aliases.items()):
            if target == primary_key:
                self._aliases.pop(alias, None)

    def metadata_for_event(
        self,
        event: DomainEvent,
        *,
        market: Market | None = None,
    ) -> dict[str, Any]:
        return self.metadata_for(
            condition_id=event.condition_id or (market.condition_id if market is not None else None),
            market_slug=event.market_slug or (market.market_slug if market is not None else None),
            event_slug=event.event_slug or (market.event_slug if market is not None else None),
        )


def _record_identity_keys(record: EntryMetadataRecord) -> tuple[str, ...]:
    """返回同一 metadata 事实可被查询的所有市场身份。"""

    keys: list[str] = []
    if record.condition_id:
        keys.append(_identity(condition_id=record.condition_id))
    if record.market_slug:
        keys.append(_identity(market_slug=record.market_slug))
    if record.event_slug:
        keys.append(_identity(event_slug=record.event_slug))
    return tuple(keys)
