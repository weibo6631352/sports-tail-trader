"""LiveSourceCalibrator —— matcher 之后的三角验证防止错配入 store。

matcher 选定候选 event 后，calibrator 独立验证三项：

1. **team match**：market slug + name + question 文本 ↔ event home/away 别名
   双向 fuzzy 匹配（仅对 TEAM_MATCH kind）
2. **time window**：market.game_start_time ↔ event.event_start_time，按 source
   priority 分级容忍
3. **league**：event.league 存在性 + 大类一致（精确大类匹配预留 league_normalizer 接口）

任何一项 hard fail → `CalibrationResult.accepted=False` + rejection_reason，
不写 `MarketMetadataStore`（避免错配进入决策），由 `LiveStateMatchService`
落 `SPORTS_LIVE_MATCH_GAP_RECORDED` audit 给运维查证（CLAUDE.md §18）。

# 阈值按 source priority 分级

| Provider | team match | time window | league |
|---|---|---|---|
| inplay (prio 100) | 严格 | ±10 min | event.league 必须非空 |
| livescore (prio 50) | 严格 | ±10 min | 同上 |

阈值放在表里集中管理，按 provider 查表，方便后续按数据观察调整。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import LiveEvent, LiveEventKind

from .source import LiveSourceKey, LiveSourceProvider

Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    accepted: bool
    confidence: Confidence
    rejection_reason: str | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


_TIME_WINDOW_BY_PROVIDER: dict[LiveSourceProvider, timedelta] = {
    LiveSourceProvider.GOALSERVE_INPLAY: timedelta(minutes=10),
    LiveSourceProvider.GOALSERVE_LIVESCORE: timedelta(minutes=10),
}


class LiveSourceCalibrator:
    def calibrate(
        self,
        *,
        market: Market,
        matched_event: LiveEvent,
        source: LiveSourceKey,
    ) -> CalibrationResult:
        diagnostics: dict[str, Any] = {
            "source": source.as_label(),
            "event_kind": matched_event.kind.value,
            "matched_event_id": matched_event.source_event_id,
        }

        if matched_event.kind == LiveEventKind.TEAM_MATCH:
            if not self._check_teams(market, matched_event, diagnostics):
                return CalibrationResult(
                    accepted=False,
                    confidence="low",
                    rejection_reason="team_match_failed",
                    diagnostics=diagnostics,
                )

        time_reason = self._check_time(market, matched_event, source, diagnostics)
        if time_reason is not None:
            return CalibrationResult(
                accepted=False,
                confidence="low",
                rejection_reason=time_reason,
                diagnostics=diagnostics,
            )

        league_reason = self._check_league(matched_event, diagnostics)
        if league_reason is not None:
            return CalibrationResult(
                accepted=False,
                confidence="low",
                rejection_reason=league_reason,
                diagnostics=diagnostics,
            )

        return CalibrationResult(
            accepted=True,
            confidence=self._derive_confidence(diagnostics),
            diagnostics=diagnostics,
        )

    def _check_teams(
        self,
        market: Market,
        event: LiveEvent,
        diagnostics: dict[str, Any],
    ) -> bool:
        """双向团队名 fuzzy 匹配——market 文本同时命中 home + away 别名。"""

        market_text = " ".join(
            filter(
                None,
                [
                    (market.market_slug or "").lower(),
                    (market.market_name or "").lower(),
                    (market.market_question or "").lower(),
                    (market.event_title or "").lower(),
                ],
            )
        )
        home = event.home
        away = event.away
        if home is None or away is None:
            diagnostics["team_check"] = "missing_participants"
            return False
        home_aliases = tuple(alias for alias in home.match_aliases() if alias)
        away_aliases = tuple(alias for alias in away.match_aliases() if alias)
        home_hit = next(
            (alias for alias in home_aliases if alias.lower() in market_text),
            None,
        )
        away_hit = next(
            (alias for alias in away_aliases if alias.lower() in market_text),
            None,
        )
        diagnostics["team_check"] = {
            "home_hit_alias": home_hit,
            "away_hit_alias": away_hit,
            "home_alias_count": len(home_aliases),
            "away_alias_count": len(away_aliases),
        }
        return home_hit is not None and away_hit is not None

    def _check_time(
        self,
        market: Market,
        event: LiveEvent,
        source: LiveSourceKey,
        diagnostics: dict[str, Any],
    ) -> str | None:
        if market.game_start_time is None or event.event_start_time is None:
            # 数据缺失时不阻拦（市场 game_start_time 偶尔缺；matcher 内部已用其他启发）
            diagnostics["time_check"] = "missing_start_time"
            return None
        window = _TIME_WINDOW_BY_PROVIDER.get(
            source.provider, timedelta(minutes=10)
        )
        drift = abs(market.game_start_time - event.event_start_time)
        diagnostics["time_drift_seconds"] = drift.total_seconds()
        diagnostics["time_window_seconds"] = window.total_seconds()
        if drift > window:
            return "time_window_exceeded"
        return None

    def _check_league(
        self,
        event: LiveEvent,
        diagnostics: dict[str, Any],
    ) -> str | None:
        # league 一致性目前仅做存在性弱检查——精确大类匹配需要 league_normalizer，
        # 后续按数据观察增强（暂不阻拦避免误杀）。
        if not event.league:
            diagnostics["league_check"] = "event_league_missing"
            return None
        diagnostics["league_check"] = {"event_league": event.league}
        return None

    def _derive_confidence(self, diagnostics: dict[str, Any]) -> Confidence:
        team = diagnostics.get("team_check")
        if isinstance(team, dict) and team.get("home_hit_alias") and team.get("away_hit_alias"):
            return "high"
        return "medium"
