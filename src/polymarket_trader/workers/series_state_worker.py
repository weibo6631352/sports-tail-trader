"""系列赛热态同步 worker（P2，低频）。

针对追踪的 series WINNER market 周期性拉取系列赛比分 + 剩余场次，写入
``EntryMetadataStore`` 的 ``series_state`` 键。series evaluator 在决策路径读
这份内存快照、不发起 IO。

cadence 默认 600s（10 分钟）；比赛中实际波动通常是局间，足够。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.infra.sports.series_state_client import SeriesStateClient
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.serialization import jsonable
from strategies.current.series.types import SeriesState


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


SportKeyResolver = Callable[[Market], str | None]
SeriesKeyResolver = Callable[[Market], str | None]


class SeriesStateWorker:
    """周期拉取追踪 series winner market 的系列赛热态。"""

    priority = "P2"

    def __init__(
        self,
        *,
        client: SeriesStateClient,
        registry: MarketRegistry,
        entry_metadata_store: EntryMetadataStore,
        sport_key_for: SportKeyResolver,
        is_series_winner_market: Callable[[Market], bool],
        series_key_for: SeriesKeyResolver,
        ttl_seconds: int = 600,
        enabled: bool = False,
    ) -> None:
        self._client = client
        self._registry = registry
        self._entry_metadata_store = entry_metadata_store
        self._sport_key_for = sport_key_for
        self._is_series_winner_market = is_series_winner_market
        self._series_key_for = series_key_for
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._enabled = enabled
        self._last_fetched_at: dict[str, datetime] = {}
        self._last_started_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._last_markets_seen = 0
        self._last_markets_refreshed = 0

    async def sync_once(self) -> int:
        if not self._enabled:
            return 0
        self._last_started_at = _utc_now()
        self._last_error = None
        markets = self._registry.snapshot().markets
        targets = tuple(m for m in markets if self._is_series_winner_market(m))
        self._last_markets_seen = len(targets)
        refreshed = 0
        for market in targets:
            sport_key = self._sport_key_for(market)
            if not sport_key:
                continue
            series_key = self._series_key_for(market)
            if not series_key:
                continue
            if not self._should_refresh(market.condition_id):
                continue
            try:
                state = await self._client.fetch(
                    sport_key=sport_key,
                    series_key=series_key,
                )
            except Exception as exc:
                self._consecutive_failures += 1
                self._last_error = f"{market.market_slug}: {exc}"
                continue
            self._consecutive_failures = 0
            self._last_fetched_at[market.condition_id] = _utc_now()
            if state is None:
                continue
            self._upsert(market, state)
            refreshed += 1
        self._last_markets_refreshed = refreshed
        return refreshed

    def _should_refresh(self, condition_id: str) -> bool:
        last = self._last_fetched_at.get(condition_id)
        if last is None:
            return True
        return (_utc_now() - last).total_seconds() >= self._ttl_seconds

    def _upsert(self, market: Market, state: SeriesState) -> None:
        # EntryMetadataStore.upsert 是整记录替换，必须保留既有 live_state_* /
        # season_odds 等字段，否则 series_state 写入会清掉同 market 的其他元数据。
        payload = dict(jsonable(state))
        existing_record = self._entry_metadata_store.find(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
        )
        existing_metadata: dict = {}
        existing_live_state_allowed = None
        existing_live_state_reason = ""
        existing_live_state_phase = ""
        existing_live_state_payload: dict = {}
        if existing_record is not None:
            existing_metadata = dict(existing_record.metadata)
            existing_live_state_allowed = existing_record.live_state_signal_allowed
            existing_live_state_reason = existing_record.live_state_signal_reason or ""
            existing_live_state_phase = existing_record.live_state_phase or ""
            existing_live_state_payload = dict(existing_record.live_state_payload or {})
        existing_metadata["series_state"] = payload
        self._entry_metadata_store.upsert(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
            source="series_state:espn",
            updated_at=state.observed_at,
            metadata=existing_metadata,
            live_state_signal_allowed=existing_live_state_allowed,
            live_state_signal_reason=existing_live_state_reason,
            live_state_phase=existing_live_state_phase,
            live_state_payload=existing_live_state_payload,
        )

    def status_snapshot(self) -> Mapping[str, object]:
        return {
            "enabled": self._enabled,
            "last_started_at": self._last_started_at.isoformat() if self._last_started_at else None,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
            "last_markets_seen": self._last_markets_seen,
            "last_markets_refreshed": self._last_markets_refreshed,
            "ttl_seconds": self._ttl_seconds,
        }
