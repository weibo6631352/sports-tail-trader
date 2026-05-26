"""SportsQueryAggregator —— 体育直播状态相关只读查询（含 outright resolution）。

替代 `app/admin_query/sports.py:AdminSportsQueryMixin` 全部方法。

按 docs/新架构方案.md §12.2：
- list_sports_live_events_history → DB audit 查询
- list_sports_live_states / list_sports_live_source_gaps → 内存运营查询
- outright_team_resolution → strategy 内部诊断

注意 SportsLiveAggregator 已存在但 API 形态不同（基于 LiveStateStore +
LiveSourceRegistry）；此 aggregator 走 metadata_store + registry，覆盖
原 admin_service sports 路由的完整契约。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from polymarket_trader.app.admin_serialization import AdminSerializer, page_payload
from polymarket_trader.app.admin_service_helpers import (
    _live_source_gap_market_payload,
    _live_source_gap_outside_diagnostic_window,
    _live_source_gap_scope_markets,
    _live_source_gap_urgency,
    _live_source_gap_urgency_rank,
    _market_slug_prefix,
)
from polymarket_trader.config import Settings
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage

from .timeline_aggregator import TimelineAggregator

if TYPE_CHECKING:
    pass


class SportsQueryAggregator:
    def __init__(
        self,
        *,
        runtime: Any,
        serializer: AdminSerializer | None = None,
    ) -> None:
        self._runtime = runtime
        self._session_factory = getattr(runtime, "db_session_factory", None) if runtime else None
        self._serializer = serializer or AdminSerializer()
        self._timeline = TimelineAggregator(
            session_factory=self._session_factory,
            runtime=runtime,
            serializer=self._serializer,
        )

    def _entry_metadata_store(self) -> Any | None:
        return getattr(self._runtime, "market_metadata_store", None) if self._runtime else None

    @staticmethod
    def _slice_sequence(
        items: Sequence[Any], *, limit: int, offset: int
    ) -> RepositoryPage[Any]:
        if limit <= 0:
            limit = 100
        if offset < 0:
            offset = 0
        sliced = tuple(items[offset : offset + limit])
        return RepositoryPage(items=sliced, total=len(items), limit=limit, offset=offset)

    async def list_sports_live_events_history(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        return await self._timeline.list_audit_events(
            limit=limit,
            offset=offset,
            event_title=DomainEventType.SPORTS_LIVE_STATE_RECORDED.value,
            condition_id=condition_id,
            time_range=time_range,
        )

    async def list_sports_live_states(
        self, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        store = self._entry_metadata_store()
        records = (
            ()
            if store is None
            else tuple(record for record in store.records() if bool(record.live_state_payload))
        )
        records = tuple(
            sorted(
                records,
                key=lambda r: 0 if str(r.live_state_phase or "").lower() == "live" else 1,
            )
        )
        page = self._slice_sequence(records, limit=limit, offset=offset)
        return page_payload(page, serializer=lambda record: record.as_payload())

    async def list_sports_live_source_gaps(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        prefix: str | None = None,
        include_future_schedule: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        registry = self._runtime.registry if self._runtime else None
        store = self._entry_metadata_store()
        if registry is None:
            empty = page_payload(
                self._slice_sequence((), limit=limit, offset=offset),
                serializer=lambda item: item,
            )
            empty.update(
                {
                    "tracked_markets": 0,
                    "live_state_markets": 0,
                    "missing_live_state_markets": 0,
                    "by_prefix": [],
                }
            )
            return empty

        all_markets = tuple(registry.snapshot().markets)
        markets = _live_source_gap_scope_markets(self._runtime, all_markets)
        scoped_condition_ids = {market.condition_id for market in markets}
        live_state_records = (
            ()
            if store is None
            else tuple(
                record
                for record in store.records()
                if bool(record.live_state_payload)
                and record.condition_id in scoped_condition_ids
            )
        )
        if now is None:
            now = datetime.now(timezone.utc)
        settings = self._runtime.settings if self._runtime else None
        league_codes = (
            settings.sports_live_state_league_codes if isinstance(settings, Settings) else ()
        )
        supported_league_prefixes: frozenset[str] | None = (
            frozenset(code.lower() for code in league_codes if code) or None
        )

        missing_with_urgency: list[tuple[Market, str]] = []
        deferred_future_schedule_count = 0
        normalized_prefix = None if prefix is None else prefix.strip().lower()
        for market in markets:
            record = (
                None
                if store is None
                else store.find(
                    condition_id=market.condition_id,
                    market_slug=market.market_slug,
                    event_slug=market.event_slug,
                )
            )
            if record is not None and bool(record.live_state_payload):
                continue
            market_prefix = _market_slug_prefix(market)
            if normalized_prefix and market_prefix != normalized_prefix:
                continue
            urgency = _live_source_gap_urgency(
                market,
                now=now,
                supported_league_prefixes=supported_league_prefixes,
            )
            if _live_source_gap_outside_diagnostic_window(market, now=now):
                continue
            if urgency == "future_schedule" and not include_future_schedule:
                deferred_future_schedule_count += 1
                continue
            missing_with_urgency.append((market, urgency))

        missing_with_urgency.sort(
            key=lambda entry: (
                _live_source_gap_urgency_rank(entry[1]),
                entry[0].game_start_time or datetime.max.replace(tzinfo=timezone.utc),
                entry[0].market_slug or entry[0].condition_id,
            ),
        )
        missing_markets = [market for market, _ in missing_with_urgency]
        by_prefix_counts: dict[str, int] = {}
        by_urgency_counts: dict[str, int] = {}
        for market, urgency in missing_with_urgency:
            market_prefix = _market_slug_prefix(market)
            by_prefix_counts[market_prefix] = by_prefix_counts.get(market_prefix, 0) + 1
            by_urgency_counts[urgency] = by_urgency_counts.get(urgency, 0) + 1
        by_prefix = [
            {"prefix": item_prefix, "count": count}
            for item_prefix, count in sorted(
                by_prefix_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        by_urgency = [
            {"urgency": urgency, "count": count}
            for urgency, count in sorted(
                by_urgency_counts.items(),
                key=lambda item: (_live_source_gap_urgency_rank(item[0]), item[0]),
            )
        ]

        page = self._slice_sequence(tuple(missing_markets), limit=limit, offset=offset)
        payload = page_payload(
            page,
            serializer=lambda market: _live_source_gap_market_payload(
                market,
                now=now,
                supported_league_prefixes=supported_league_prefixes,
            ),
        )
        payload.update(
            {
                "tracked_markets": len(markets),
                "total_tracked_markets": len(all_markets),
                "live_state_markets": len(live_state_records),
                "missing_live_state_markets": len(missing_markets),
                "deferred_future_schedule_markets": deferred_future_schedule_count,
                "by_prefix": by_prefix,
                "by_urgency": by_urgency,
                "prefix": normalized_prefix,
                "include_future_schedule": include_future_schedule,
            }
        )
        return payload

    async def outright_team_resolution(
        self,
        *,
        condition_id: str | None = None,
        market_slug: str | None = None,
    ) -> dict[str, Any] | None:
        """对指定 outright market 跑 resolve_market_team_debug，返回 trace。"""

        strategy = self._runtime.workflow if self._runtime else None
        if strategy is None or not hasattr(strategy, "resolve_outright_team_debug_payload"):
            return None
        registry = self._runtime.registry if self._runtime else None
        if registry is None:
            return None
        market: Market | None = None
        if condition_id:
            market = registry.get_by_condition_id(condition_id)
        if market is None and market_slug:
            market = registry.get_by_slug(market_slug)
        if market is None:
            return None
        return strategy.resolve_outright_team_debug_payload(market)
