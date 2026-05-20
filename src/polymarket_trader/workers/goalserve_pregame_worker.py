"""Goalserve 赛前赔率同步 worker（P2，增量拉取）。

周期性调用 GoalservePregameOddsClient.fetch_all()，用 ts 增量减少流量；
快照存内存，同时把能匹配到市场的 pregame_moneyline 写入 EntryMetadataStore，
供策略 gates.py 在 goalserve_moneyline（inplay）缺失时作为 fallback 验证信号。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from polymarket_trader.infra.sports.goalserve_pregame_client import (
    GoalservePregameOddsClient,
    GoalservePregameSnapshot,
    PregameMatch,
)
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_team(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


class GoalservePregameWorker:
    """周期性拉取 Goalserve 赛前赔率，供策略层按需查询。

    ts 增量：每次成功拉取后保存各运动的 ts，下次携带以减少数据量。
    单运动失败不影响其他运动（客户端内部已处理）。

    若注入 registry + entry_metadata_store，sync_once 会在拉取完成后，按
    市场的 live_game 团队名匹配 pregame match，将 pregame_moneyline 写入
    EntryMetadataStore。gates.py 可以在 goalserve_moneyline（inplay）缺失
    时 fallback 到 pregame_moneyline 完成交叉验证。
    """

    priority = "P2"

    def __init__(
        self,
        *,
        client: GoalservePregameOddsClient,
        enabled: bool = False,
        registry: MarketRegistry | None = None,
        entry_metadata_store: EntryMetadataStore | None = None,
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._registry = registry
        self._entry_metadata_store = entry_metadata_store
        self._snapshots: dict[str, GoalservePregameSnapshot] = {}
        self._ts_by_sport: dict[str, str] = {}
        self._last_started_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._last_sports_seen = 0
        self._last_matches_seen = 0
        self._last_markets_written = 0

    async def sync_once(self) -> int:
        """拉取所有启用运动的赛前赔率；返回本次拉取到的 match 总数。"""

        if not self._enabled:
            return 0
        self._last_started_at = _utc_now()
        self._last_error = None
        try:
            snapshots = await self._client.fetch_all(ts_by_sport=self._ts_by_sport)
        except Exception as exc:
            self._consecutive_failures += 1
            self._last_error = str(exc)
            logger.warning("goalserve_pregame_worker: fetch_all failed: %s", exc)
            return 0

        self._consecutive_failures = 0
        total_matches = 0
        for sport, snapshot in snapshots.items():
            self._snapshots[sport] = snapshot
            if snapshot.ts:
                self._ts_by_sport[sport] = snapshot.ts
            total_matches += len(snapshot.matches)

        self._last_sports_seen = len(snapshots)
        self._last_matches_seen = total_matches
        self._last_markets_written = self._write_pregame_metadata()
        self._last_completed_at = _utc_now()
        logger.debug(
            "goalserve_pregame_worker: sync done sports=%d matches=%d markets_written=%d",
            len(snapshots),
            total_matches,
            self._last_markets_written,
        )
        return total_matches

    def _write_pregame_metadata(self) -> int:
        """把 pregame_moneyline 写入有 live_game 的市场的 EntryMetadataStore 记录。

        匹配逻辑：从 live_game.home_name / away_name 规范化后在 pregame 索引中查找。
        只更新已有记录的 pregame_moneyline 字段，不创建新记录，不改变 live_state 字段。
        """

        if self._registry is None or self._entry_metadata_store is None:
            return 0

        index = self._build_team_index()
        if not index:
            return 0

        written = 0
        markets = self._registry.snapshot().markets
        for market in markets:
            record = self._entry_metadata_store.find(
                condition_id=market.condition_id,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
            )
            if record is None:
                continue
            raw_game = record.metadata.get("live_game")
            if not isinstance(raw_game, dict):
                continue
            home_name = raw_game.get("home_name")
            away_name = raw_game.get("away_name")
            if not home_name or not away_name:
                continue

            pregame_match = _lookup_match(index, home_name, away_name)
            if pregame_match is None:
                continue
            pregame_ml = _extract_pregame_moneyline(pregame_match)
            if pregame_ml is None:
                continue

            existing_metadata = dict(record.metadata)
            existing_metadata["pregame_moneyline"] = pregame_ml
            self._entry_metadata_store.upsert(
                condition_id=market.condition_id,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
                source=record.source,
                updated_at=record.updated_at,
                metadata=existing_metadata,
                live_state_signal_allowed=record.live_state_signal_allowed,
                live_state_signal_reason=record.live_state_signal_reason or "",
                live_state_phase=record.live_state_phase or "",
                live_state_payload=dict(record.live_state_payload or {}),
            )
            written += 1

        return written

    def _build_team_index(self) -> dict[str, PregameMatch]:
        """构建 {normalized_home|normalized_away: PregameMatch} 双向索引。"""

        index: dict[str, PregameMatch] = {}
        for snapshot in self._snapshots.values():
            for match in snapshot.matches:
                home_key = _normalize_team(match.home_team)
                away_key = _normalize_team(match.away_team)
                if home_key and away_key:
                    pair = f"{home_key}|{away_key}"
                    index[pair] = match
        return index

    def get_snapshots(self) -> dict[str, GoalservePregameSnapshot]:
        """返回最新赛前赔率快照（{sport: snapshot}），供策略层只读查询。"""

        return dict(self._snapshots)

    def get_snapshot(self, sport: str) -> GoalservePregameSnapshot | None:
        """按运动名返回快照；不存在返回 None。"""

        return self._snapshots.get(sport.lower().strip())

    def status_snapshot(self) -> Mapping[str, Any]:
        return {
            "enabled": self._enabled,
            "last_started_at": self._last_started_at.isoformat() if self._last_started_at else None,
            "last_completed_at": self._last_completed_at.isoformat() if self._last_completed_at else None,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
            "sports_seen": self._last_sports_seen,
            "matches_seen": self._last_matches_seen,
            "markets_written": self._last_markets_written,
            "sports_cached": list(self._snapshots.keys()),
        }


def _lookup_match(
    index: dict[str, PregameMatch],
    home_name: str,
    away_name: str,
) -> PregameMatch | None:
    """规范化后在双向索引中查找。"""

    home_key = _normalize_team(home_name)
    away_key = _normalize_team(away_name)
    return index.get(f"{home_key}|{away_key}")


_ML_OUTCOME_HOME = frozenset({"home", "1", "home team"})
_ML_OUTCOME_AWAY = frozenset({"away", "2", "away team"})


def _extract_pregame_moneyline(match: PregameMatch) -> dict[str, Any] | None:
    """从 PregameMatch 提取 moneyline，与 goalserve_moneyline 格式对齐。

    返回 None 表示没有可用赔率（盘口不存在或全部暂停）。
    source 字段标记为 goalserve_pregame，供审计区分 inplay 与 pregame 两个来源。
    """

    ml_market = match.moneyline_market()
    if ml_market is None or ml_market.suspended:
        return None

    outcomes = ml_market.outcomes
    home_outcome = next(
        (o for o in outcomes if o.name.lower() in _ML_OUTCOME_HOME and not o.suspended), None
    )
    away_outcome = next(
        (o for o in outcomes if o.name.lower() in _ML_OUTCOME_AWAY and not o.suspended), None
    )
    if home_outcome is None or away_outcome is None:
        return None

    try:
        home_eu = float(home_outcome.value_eu)
        away_eu = float(away_outcome.value_eu)
        if home_eu <= 0 or away_eu <= 0:
            return None
        home_implied = round(1.0 / home_eu, 6)
        away_implied = round(1.0 / away_eu, 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    return {
        "market_name": ml_market.name,
        "home_eu": home_eu,
        "away_eu": away_eu,
        "home_implied_prob": home_implied,
        "away_implied_prob": away_implied,
        "suspended": False,
        "home_suspended": bool(home_outcome.suspended),
        "away_suspended": bool(away_outcome.suspended),
        "source": "goalserve_pregame",
    }
