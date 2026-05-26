"""SportsQueryAggregator —— 体育直播状态相关只读查询，全部走内存。

按 原架构方案 §12.2 + 用户原则"能内存就内存,尽量不 DB"：
- list_sports_live_events_history → ``SportsLiveHistoryBuffer`` 内存 ring buffer
  （远期历史走 /audit-events/by-condition/{cid}?channels=sports_live_state_recorded）
- list_sports_live_states / list_sports_live_source_gaps → market_metadata_store + registry

以 market_metadata_store + sports_live_history_buffer 为真相源,覆盖前端 operator
sports 路由的完整契约。零 DB。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from polymarket_trader.api.serialization import ApiSerializer
from polymarket_trader.config import Settings
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.serialization import page_payload

from ._helpers import slice_sequence

# 直播源缺口诊断窗口：开赛已超过此时长的市场视为陈旧，不再算缺口。
# 设 6h 覆盖单场赛事全长（足球/篮球/棒球/网球/橄榄球均 < 5h）。
_LIVE_SOURCE_GAP_PAST_WINDOW = timedelta(hours=6)


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _market_slug_prefix(market: Market) -> str:
    """提取 market slug 首段，用于直播源缺口聚合分组。"""
    slug = (market.market_slug or market.event_slug or "").strip().lower()
    if not slug:
        return "unknown"
    return slug.split("-", 1)[0] or "unknown"


def _runtime_workflow(runtime: Any) -> Any | None:
    try:
        return runtime.workflow
    except (RuntimeError, AttributeError):
        pass
    try:
        return runtime.market_ingest_service.workflow
    except AttributeError:
        return None


def _live_source_gap_scope_markets(
    runtime: Any, markets: "Sequence[Market]",
) -> tuple[Market, ...]:
    """单场直播源覆盖诊断 = 仅 family == single_game 的市场子集。

    系列赛/冠军/奖项/转会等长期市场不依赖单场直播源，避免缺口噪声污染。
    """
    workflow = _runtime_workflow(runtime)
    if workflow is None:
        return tuple(markets)
    scoped: list[Market] = []
    for market in markets:
        try:
            decision = workflow.select_market(market)
        except Exception:  # noqa: BLE001
            continue
        if not decision.selected:
            continue
        family = (
            workflow.market_family_label(market)
            if hasattr(workflow, "market_family_label") else None
        )
        if family is not None and family != "single_game":
            continue
        scoped.append(market)
    return tuple(scoped)


def _live_source_gap_urgency(
    market: Market,
    *,
    now: datetime,
    supported_league_prefixes: frozenset[str] | None = None,
) -> str:
    if supported_league_prefixes is not None:
        prefix = _market_slug_prefix(market)
        if prefix and prefix != "unknown" and prefix not in supported_league_prefixes:
            return "unsupported_league"
    start_time = _ensure_utc(market.game_start_time)
    if start_time is None:
        return "unknown_time"
    if start_time <= now:
        return "started_or_past_due"
    if start_time <= now + timedelta(hours=24):
        return "starts_within_24h"
    return "future_schedule"


def _live_source_gap_outside_diagnostic_window(
    market: Market, *, now: datetime,
) -> bool:
    start_time = _ensure_utc(market.game_start_time)
    if start_time is None:
        return False
    return start_time < now - _LIVE_SOURCE_GAP_PAST_WINDOW


def _live_source_gap_urgency_rank(urgency: str) -> int:
    """直播源缺口优先级排序权重——越小越紧迫。unsupported_league 排最后。"""
    ranks = {
        "started_or_past_due": 0,
        "starts_within_24h": 1,
        "future_schedule": 2,
        "unknown_time": 3,
        "unsupported_league": 98,
    }
    return ranks.get(urgency, 99)


def _live_source_gap_market_payload(
    market: Market,
    *,
    now: datetime,
    supported_league_prefixes: frozenset[str] | None = None,
) -> dict[str, Any]:
    """把缺少直播状态的 market 转成诊断样本。"""
    start_time = _ensure_utc(market.game_start_time)
    end_date = _ensure_utc(market.end_date)
    return {
        "condition_id": market.condition_id,
        "market_slug": market.market_slug,
        "event_slug": market.event_slug,
        "slug_prefix": _market_slug_prefix(market),
        "gap_urgency": _live_source_gap_urgency(
            market, now=now, supported_league_prefixes=supported_league_prefixes,
        ),
        "game_start_time": None if start_time is None else start_time.isoformat(),
        "end_date": None if end_date is None else end_date.isoformat(),
        "market_question": market.market_question,
        "event_title": market.event_title,
        "category": market.category,
        "tags": tuple(market.tags),
        "trading_status": market.trading_status.value,
        "outcome_count": len(market.outcomes),
    }

if TYPE_CHECKING:
    pass


class SportsQueryAggregator:
    def __init__(
        self,
        *,
        runtime: Any,
        serializer: ApiSerializer | None = None,
    ) -> None:
        self._runtime = runtime
        self._serializer = serializer or ApiSerializer.from_runtime(None)

    def _entry_metadata_store(self) -> Any | None:
        return getattr(self._runtime, "market_metadata_store", None) if self._runtime else None

    def list_sports_live_events_history(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """SPORTS_LIVE_STATE_RECORDED 事件历史——从 ``sports_live_history_buffer``
        内存 ring buffer 读,零 DB。

        每 condition 默认保留 200 条（按 deduper 30s 窗口约 ~100 分钟）;
        远期历史走 ``GET /audit-events/by-condition/{cid}?channels=sports_live_state_recorded``。
        """
        buffer = getattr(self._runtime, "sports_live_history_buffer", None) if self._runtime else None
        if buffer is None:
            return {"items": (), "total": 0, "limit": limit, "offset": offset}
        since_iso = until_iso = None
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                since_iso = since_dt.isoformat()
            if until_dt is not None:
                until_iso = until_dt.isoformat()
        items, total = buffer.list_events(
            condition_id=condition_id,
            limit=limit,
            offset=offset,
            since_iso=since_iso,
            until_iso=until_iso,
        )
        return {"items": list(items), "total": total, "limit": limit, "offset": offset}

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
        page = slice_sequence(records, limit=limit, offset=offset)
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
                slice_sequence((), limit=limit, offset=offset),
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

        page = slice_sequence(tuple(missing_markets), limit=limit, offset=offset)
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

