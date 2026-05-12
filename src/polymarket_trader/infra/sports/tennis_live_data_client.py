"""api-tennis.com 商业网球直播适配器。

参考文档 https://api.api-tennis.com/documentation：
- ``GET /?method=get_livescore&APIkey=<token>`` 返回当前 livescore 列表；
- ``GET /?method=get_fixtures&APIkey=<token>&date_start=...&date_stop=...``
  返回区间内的赛程；
- payload 顶层 ``{"success":1,"result":[ { ...event... } ]}``，每个 event 含
  ``event_key`` / ``event_first_player`` / ``event_second_player`` / ``event_winner``
  等字段，盘分在 ``scores`` 数组里按局展开。

token 通过构造参数传入；未配置 token 时 ``enabled=False``，主路径不装配该 client。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
    TennisGameState,
)
from polymarket_trader.infra.sports.common import (
    SportsDataRateLimitError,
    first_text,
    int_value,
    json_mapping_from_response,
    normalize_sports_data_error,
    utc_now,
)

_DEFAULT_BASE_URL = "https://api.api-tennis.com/tennis"


class TennisLiveDataClient:
    """读取 api-tennis.com livescore 并归一化成 ``LiveEvent(sport="tennis")``。

    特殊点：网球 ``home/away`` 是单个选手；league 取自 ``tournament_name``，再用
    粗粒度文本映射回 ATP/WTA/Challenger/ITF。``external_ids`` 同时填上 atp_id /
    wta_id（payload 提供时）以便跨源合并。
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        api_token: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        min_fetch_interval_s: float = 20.0,
        max_stale_on_error_s: float = 300.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token.strip() if isinstance(api_token, str) else None
        self._timeout_s = max(0.1, float(timeout_s))
        self._min_fetch_interval_s = max(0.0, float(min_fetch_interval_s))
        self._max_stale_on_error_s = max(0.0, float(max_stale_on_error_s))
        self._now_provider = now_provider
        self._cached_snapshot: SportsLiveSnapshot | None = None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_s,
            headers={
                "accept": "application/json",
                "user-agent": "sports-tail-trader/0.1",
            },
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def enabled(self) -> bool:
        """缺 token 视为未启用；装配侧据此屏蔽 provider。"""

        return bool(self._api_token)

    async def list_events(self) -> SportsLiveSnapshot:
        observed_at = utc_now(self._now_provider)
        if not self.enabled:
            return _snapshot_with_status(
                SportsLiveSnapshot(source="tennis_live_data", observed_at=observed_at, events=()),
                health=SportsLiveSourceHealth.FAILED,
                success=False,
                last_error="tennis_live_data_token_missing",
            )
        if self._is_cache_fresh(observed_at):
            return _snapshot_with_status(
                self._cached_snapshot or SportsLiveSnapshot(source="tennis_live_data", observed_at=observed_at, events=()),
                health=SportsLiveSourceHealth.CACHED,
            )
        try:
            payload = await self._get_livescore()
        except Exception as exc:
            if self._is_cache_usable_after_error(observed_at):
                return _snapshot_with_status(
                    self._cached_snapshot or SportsLiveSnapshot(source="tennis_live_data", observed_at=observed_at, events=()),
                    health=SportsLiveSourceHealth.CACHED,
                )
            if isinstance(exc, SportsDataRateLimitError):
                snapshot = _snapshot_with_status(
                    SportsLiveSnapshot(source="tennis_live_data", observed_at=observed_at, events=()),
                    health=SportsLiveSourceHealth.RATE_LIMITED,
                    success=False,
                    last_error=str(exc),
                )
                self._cached_snapshot = snapshot
                return snapshot
            raise
        events = parse_tennis_live_data_payload(payload, observed_at=observed_at)
        snapshot = SportsLiveSnapshot(
            source="tennis_live_data",
            observed_at=observed_at,
            events=events,
        )
        snapshot = _snapshot_with_status(
            snapshot,
            health=(
                SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
                if snapshot.events
                else SportsLiveSourceHealth.SUCCESS_EMPTY
            ),
        )
        self._cached_snapshot = snapshot
        return snapshot

    def _is_cache_fresh(self, observed_at: datetime) -> bool:
        if self._cached_snapshot is None:
            return False
        return _snapshot_age_seconds(self._cached_snapshot, observed_at) < self._min_fetch_interval_s

    def _is_cache_usable_after_error(self, observed_at: datetime) -> bool:
        if self._cached_snapshot is None:
            return False
        return _snapshot_age_seconds(self._cached_snapshot, observed_at) <= self._max_stale_on_error_s

    async def _get_livescore(self) -> Mapping[str, Any]:
        operation = "tennis_live_data_livescore"
        try:
            response = await self._client.get(
                "/",
                params={"method": "get_livescore", "APIkey": self._api_token or ""},
            )
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        return json_mapping_from_response(response, operation=operation)


def parse_tennis_live_data_payload(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> tuple[LiveEvent, ...]:
    """把 api-tennis livescore payload 转成 ``LiveEvent`` 列表。"""

    observed_at = observed_at or datetime.now(timezone.utc)
    result = payload.get("result")
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
        return ()
    events: list[LiveEvent] = []
    for raw_event in result:
        if not isinstance(raw_event, Mapping):
            continue
        event = _parse_event(raw_event, observed_at=observed_at)
        if event is not None:
            events.append(event)
    return tuple(events)


def _parse_event(raw_event: Mapping[str, Any], *, observed_at: datetime) -> LiveEvent | None:
    first_player = first_text(raw_event, "event_first_player")
    second_player = first_text(raw_event, "event_second_player")
    if first_player is None or second_player is None:
        return None
    event_key = first_text(raw_event, "event_key") or ""
    tournament_name = first_text(raw_event, "tournament_name") or ""
    league = _infer_league(tournament_name, first_text(raw_event, "event_type_type"))
    status = _map_status(raw_event)
    home_id = first_text(raw_event, "first_player_key")
    away_id = first_text(raw_event, "second_player_key")
    home_external_ids: dict[str, str] = {}
    if home_id:
        home_external_ids["tennis_live_data"] = home_id
    atp_id_home = first_text(raw_event, "first_player_atp_id")
    if atp_id_home:
        home_external_ids["atp_id"] = atp_id_home
    wta_id_home = first_text(raw_event, "first_player_wta_id")
    if wta_id_home:
        home_external_ids["wta_id"] = wta_id_home
    away_external_ids: dict[str, str] = {}
    if away_id:
        away_external_ids["tennis_live_data"] = away_id
    atp_id_away = first_text(raw_event, "second_player_atp_id")
    if atp_id_away:
        away_external_ids["atp_id"] = atp_id_away
    wta_id_away = first_text(raw_event, "second_player_wta_id")
    if wta_id_away:
        away_external_ids["wta_id"] = wta_id_away
    home_sets, away_sets = _final_set_scores(raw_event)
    home_total_games, away_total_games = _total_games(raw_event)
    tennis_state = TennisGameState(
        home_sets_won=home_sets,
        away_sets_won=away_sets,
        current_set=_int_value(raw_event.get("current_set")),
        home_current_set_games=_current_set_games(raw_event, side="first"),
        away_current_set_games=_current_set_games(raw_event, side="second"),
        home_total_games=home_total_games,
        away_total_games=away_total_games,
        set_scores=_set_scores(raw_event),
        home_point=first_text(raw_event, "event_first_player_serve"),
        away_point=first_text(raw_event, "event_second_player_serve"),
        first_to_serve=first_text(raw_event, "event_first_to_serve"),
        serving_side=first_text(raw_event, "event_serve"),
    )
    event_external_ids: dict[str, str] = {}
    if event_key:
        event_external_ids["tennis_live_data"] = event_key
    return LiveEvent(
        source="tennis_live_data",
        source_event_id=event_key,
        kind=LiveEventKind.TEAM_MATCH,
        league=league,
        sport="tennis",
        participants=(
            Participant(
                role="home",
                name=first_player,
                display_name=first_player,
                external_ids=home_external_ids,
                score=home_sets,
            ),
            Participant(
                role="away",
                name=second_player,
                display_name=second_player,
                external_ids=away_external_ids,
                score=away_sets,
            ),
        ),
        status=status,
        observed_at=observed_at,
        event_start_time=_parse_event_datetime(raw_event),
        event_name=tournament_name,
        external_ids=event_external_ids,
        raw_status=first_text(raw_event, "event_status") or "",
        tennis_state=tennis_state,
        source_payload={
            "sport": "tennis",
            "tournament": tournament_name,
            "event_status": raw_event.get("event_status"),
            "event_winner": raw_event.get("event_winner"),
            "event_date": raw_event.get("event_date"),
            "event_time": raw_event.get("event_time"),
        },
    )


def _infer_league(tournament_name: str, event_type: str | None) -> str:
    """tournament_name 文本映射到 ATP/WTA/Challenger/ITF/Other。"""

    text = (tournament_name or "").lower()
    if "wta" in text:
        return "WTA"
    if "challenger" in text:
        return "Challenger"
    if "itf" in text:
        return "ITF"
    if "atp" in text:
        return "ATP"
    if event_type:
        return event_type
    return "TENNIS"


def _map_status(raw_event: Mapping[str, Any]) -> SportsLiveGameStatus:
    status_text = (first_text(raw_event, "event_status") or "").strip().lower()
    if not status_text:
        return SportsLiveGameStatus.UNKNOWN
    if status_text in {"finished", "ended", "match finished"}:
        return SportsLiveGameStatus.ENDED
    if status_text in {"set 1", "set 2", "set 3", "set 4", "set 5"} or "live" in status_text:
        return SportsLiveGameStatus.LIVE
    if status_text in {"pending", "scheduled", "not started"}:
        return SportsLiveGameStatus.SCHEDULED
    if "retired" in status_text or "walkover" in status_text:
        return SportsLiveGameStatus.RETIRED
    if "postponed" in status_text:
        return SportsLiveGameStatus.POSTPONED
    if "canceled" in status_text or "cancelled" in status_text:
        return SportsLiveGameStatus.CANCELLED
    if "interrupted" in status_text or "suspended" in status_text:
        return SportsLiveGameStatus.PAUSED
    return SportsLiveGameStatus.UNKNOWN


def _set_scores(raw_event: Mapping[str, Any]) -> tuple[tuple[int, int], ...]:
    scores = raw_event.get("scores")
    if not isinstance(scores, Sequence) or isinstance(scores, (str, bytes)):
        return ()
    parsed: list[tuple[int, int]] = []
    for entry in scores:
        if not isinstance(entry, Mapping):
            continue
        home = _int_value(entry.get("score_first"))
        away = _int_value(entry.get("score_second"))
        if home is None or away is None:
            continue
        parsed.append((home, away))
    return tuple(parsed)


def _final_set_scores(raw_event: Mapping[str, Any]) -> tuple[int, int]:
    """统计已结束的 set 中的胜场数；不把进行中的 current_set 计入（防漏算返场）。

    判定"已结束"：分数差 >= 2 且任一方 >= 6，或一方 == 7（抢七）。
    """

    home = away = 0
    for entry in _set_scores(raw_event):
        if _set_score_is_final(entry[0], entry[1]):
            if entry[0] > entry[1]:
                home += 1
            elif entry[1] > entry[0]:
                away += 1
    return home, away


def _set_score_is_final(home_games: int, away_games: int) -> bool:
    winner = max(home_games, away_games)
    loser = min(home_games, away_games)
    return (winner >= 6 and winner - loser >= 2) or winner == 7


def _total_games(raw_event: Mapping[str, Any]) -> tuple[int, int]:
    home = away = 0
    for entry in _set_scores(raw_event):
        home += entry[0]
        away += entry[1]
    return home, away


def _current_set_games(raw_event: Mapping[str, Any], *, side: str) -> int | None:
    """从最新一个 set 拿当前局数。api-tennis 在赛中会同时给历史 + 当前 set。"""

    scores = raw_event.get("scores")
    if not isinstance(scores, Sequence) or isinstance(scores, (str, bytes)):
        return None
    if not scores:
        return None
    last = scores[-1]
    if not isinstance(last, Mapping):
        return None
    if side == "first":
        return _int_value(last.get("score_first"))
    return _int_value(last.get("score_second"))


def _parse_event_datetime(raw_event: Mapping[str, Any]) -> datetime | None:
    date_text = first_text(raw_event, "event_date")
    time_text = first_text(raw_event, "event_time")
    if not date_text:
        return None
    candidate = f"{date_text}T{time_text or '00:00:00'}"
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _int_value(value: Any) -> int | None:
    """容忍 api-tennis 偶发的 "6" / "6.0" / 6 等表达。"""

    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return int_value(value)


def _snapshot_with_status(
    snapshot: SportsLiveSnapshot,
    *,
    health: SportsLiveSourceHealth,
    success: bool = True,
    last_error: str | None = None,
) -> SportsLiveSnapshot:
    return SportsLiveSnapshot(
        source="tennis_live_data",
        observed_at=snapshot.observed_at,
        events=snapshot.events,
        source_statuses=(
            SportsLiveSourceStatus(
                source="tennis_live_data",
                success=success,
                health=health,
                events_seen=len(snapshot.events),
                observed_at=snapshot.observed_at,
                last_error=last_error,
            ),
        ),
    )


def _snapshot_age_seconds(snapshot: SportsLiveSnapshot, observed_at: datetime) -> float:
    age = observed_at - snapshot.observed_at
    return max(0.0, age.total_seconds())


__all__ = ["TennisLiveDataClient", "parse_tennis_live_data_payload"]
