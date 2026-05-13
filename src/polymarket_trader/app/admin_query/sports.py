"""体育直播状态相关只读查询：历史事件、当前直播状态、覆盖缺口诊断。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from polymarket_trader.app.admin_serialization import page_payload
from polymarket_trader.app.admin_service_helpers import (
    _live_source_gap_market_payload,
    _live_source_gap_outside_diagnostic_window,
    _live_source_gap_scope_markets,
    _live_source_gap_urgency,
    _live_source_gap_urgency_rank,
    _market_slug_prefix,
)
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.serialization import jsonable

# 这两段 import 跨 app → strategies 边界（与 ``infra/sports/series_state_client.py``
# 同模式）：``series_state`` 元数据键的约定属于 series 策略包，admin 诊断必须读到
# 同一份强类型解析与 team_resolver trace 才能给运维一致的诊断结果，没有 framework
# 侧的等价物可复用——所以让 admin 直接调策略包的纯函数（无副作用、无状态）。
from strategies.current.outright.match import season_odds_from_metadata
from strategies.current.outright.team_resolver import resolve_market_team_debug
from strategies.current.series.match import series_state_from_metadata


if TYPE_CHECKING:
    from polymarket_trader.app.admin_query._protocol import AdminQueryHost as _Base
else:
    _Base = object


class AdminSportsQueryMixin(_Base):
    """体育直播状态相关只读查询。

    继承 ``AdminQueryHost`` 仅在 TYPE_CHECKING 模式下生效，让 mypy 看到本 mixin
    依赖宿主 (AdminService) 提供的 ``runtime`` / ``_entry_metadata_store`` /
    ``_slice_sequence`` 等 helper；运行时仍由 AdminService 的多重继承装配实际
    方法，不引入额外间接调用。
    """

    async def list_sports_live_events_history(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """历史体育实时事件——按 audit_events 中 ``event_title='sports_live_state_recorded'`` 过滤。

        每条事件 payload 含 score / clock / phase / signal_allowed / signal_reason
        等比赛快照，复盘"决策时的比分/时钟/赛况"必备。
        """

        from polymarket_trader.domain.events import DomainEventType

        return await self.list_audit_events(
            limit=limit,
            offset=offset,
            event_title=DomainEventType.SPORTS_LIVE_STATE_RECORDED.value,
            condition_id=condition_id,
            time_range=time_range,
        )

    async def list_sports_live_states(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页返回当前运行时保存的体育直播状态 metadata。"""

        store = self._entry_metadata_store()
        records = (
            ()
            if store is None
            else tuple(record for record in store.records() if bool(record.live_state_payload))
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
        """诊断已跟踪市场中缺少体育直播状态的覆盖缺口。

        该接口只读取运行时 registry 和 metadata store，用于实盘观察直播源/
        匹配覆盖率，不触发 discovery、订阅或交易判断。
        """

        registry = getattr(self.runtime, "registry", None)
        store = self._entry_metadata_store()
        if registry is None:
            empty = page_payload(self._slice_sequence((), limit=limit, offset=offset), serializer=lambda item: item)
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
        markets = _live_source_gap_scope_markets(self.runtime, all_markets)
        scoped_condition_ids = {market.condition_id for market in markets}
        live_state_records = (
            ()
            if store is None
            else tuple(
                record
                for record in store.records()
                if bool(record.live_state_payload) and record.condition_id in scoped_condition_ids
            )
        )
        if now is None:
            now = datetime.now(timezone.utc)
        settings = getattr(self.runtime, "settings", None)
        league_codes = (
            getattr(settings, "sports_live_state_league_codes", ()) if settings is not None else ()
        )
        supported_league_prefixes: frozenset[str] | None = (
            frozenset(code.lower() for code in league_codes if code) or None
        )
        # urgency 只算一次：filter → sort → counts 共用 (Market, urgency) 元组列表。
        # 之前 3 处分别重算 _live_source_gap_urgency(market, now, supported_league_prefixes)，
        # 每条 missing market 3 次同语义运算，热路径无意义浪费。
        missing_with_urgency: list[tuple[Market, str]] = []
        deferred_future_schedule_count = 0
        normalized_prefix = None if prefix is None else prefix.strip().lower()
        for market in markets:
            record = None if store is None else store.find(
                condition_id=market.condition_id,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
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

    async def list_series_state_snapshots(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """返回 ``EntryMetadataStore`` 中所有 ``series_state`` 快照。

        线上验证 ``series_state_worker`` 是否在持续刷新比分；如果某场系列赛
        ``age_seconds`` 长期超出 ``tail_series_winner_max_state_age_seconds``，
        说明 ESPN 抓取链路出问题，evaluator 会以 ``STALE_SERIES_STATE``/
        ``MISSING_SERIES_STATE`` 拒绝该市场。
        """

        store = self._entry_metadata_store()
        if store is None:
            page = self._slice_sequence((), limit=limit, offset=offset)
            payload = page_payload(page, serializer=lambda item: item)
            payload["total_records"] = 0
            return payload
        if now is None:
            now = datetime.now(timezone.utc)
        items: list[dict[str, Any]] = []
        for record in store.records():
            state = series_state_from_metadata(record.metadata)
            if state is None:
                continue
            age_seconds = max(0.0, (now - state.observed_at).total_seconds())
            items.append(
                {
                    "condition_id": record.condition_id,
                    "market_slug": record.market_slug,
                    "event_slug": record.event_slug,
                    "team_a": state.team_a,
                    "team_b": state.team_b,
                    "wins_a": state.wins_a,
                    "wins_b": state.wins_b,
                    "best_of": state.best_of,
                    "next_game_at": jsonable(state.next_game_at),
                    "observed_at": jsonable(state.observed_at),
                    "age_seconds": age_seconds,
                    "source": record.source,
                    "updated_at": jsonable(record.updated_at),
                }
            )
        # observed_at 最新的排前面：诊断时优先看最新写入的。
        items.sort(key=lambda item: item["observed_at"] or "", reverse=True)
        page = self._slice_sequence(tuple(items), limit=limit, offset=offset)
        payload = page_payload(page, serializer=lambda item: item)
        payload["total_records"] = len(items)
        return payload

    async def outright_team_resolution(
        self,
        *,
        condition_id: str | None = None,
        market_slug: str | None = None,
    ) -> dict[str, Any] | None:
        """对指定 outright market 跑 ``resolve_market_team_debug``，返回 trace。

        线上 ``OUTRIGHT_TEAM_NOT_RESOLVED`` 拒绝原因排查入口：admin 调本接口
        看 normalized_text / candidate_teams / matches，判断是 snapshot 缺该球队
        还是文本归一化遗漏标点 / 别名。返回 ``None`` 时由路由层翻译成 404。
        """

        market = self._resolve_market(
            condition_id=condition_id,
            market_slug=market_slug,
        )
        if market is None:
            return None
        metadata = self._entry_metadata_for_market(market)
        snapshot = season_odds_from_metadata(metadata)
        if snapshot is None:
            return {
                "market": {
                    "condition_id": market.condition_id,
                    "market_slug": market.market_slug,
                    "event_slug": market.event_slug,
                    "market_question": market.market_question,
                    "event_title": market.event_title,
                },
                "snapshot_available": False,
                "reason": "missing_season_odds",
                "trace": None,
            }
        trace = resolve_market_team_debug(market, snapshot)
        return {
            "market": {
                "condition_id": market.condition_id,
                "market_slug": market.market_slug,
                "event_slug": market.event_slug,
                "market_question": market.market_question,
                "event_title": market.event_title,
            },
            "snapshot_available": True,
            "snapshot_market_key": snapshot.market_key,
            "snapshot_source": snapshot.source,
            "snapshot_observed_at": jsonable(snapshot.observed_at),
            "trace": trace.as_payload(),
        }


__all__ = ["AdminSportsQueryMixin"]
