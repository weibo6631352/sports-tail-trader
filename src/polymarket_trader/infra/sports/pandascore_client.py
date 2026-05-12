"""Pandascore 电子竞技直播比分适配器。

把 Pandascore ``/lives`` 与 ``/matches/running`` 响应归一化成内部 ``LiveEvent``。
ESPORTS family 在 SportsMarketFamily 中已分类，但今天没有任何 provider 提供
实时数据；Pandascore 是覆盖最广的 esports 数据源（CS2/Dota2/LoL/Valorant 等）。

需要 Bearer token；未配置 token 时 client 自动禁用并在 source_statuses 中以
``RATE_LIMITED + last_error`` 上报，不阻断 boot。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

from polymarket_trader.domain.sports_live import (
    EsportsGameState,
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
)
from polymarket_trader.infra.sports.common import (
    SportsDataRateLimitError,
    first_text,
    int_value,
    normalize_sports_data_error,
    utc_now,
)

_DEFAULT_BASE_URL = "https://api.pandascore.co"
_LIVES_PATH = "/lives"


class PandascoreLiveClient:
    """读取 Pandascore live 列表并归一化成 ``LiveEvent(kind=TEAM_MATCH, sport="esports")``。

    Pandascore videogame slug → league name 映射保留原 slug 大写形式
    （CS2 / DOTA2 / LOL / VALORANT / R6 / KING_OF_GLORY 等），便于策略层 league
    匹配。Source name 永远是 ``pandascore``，没有子源细分。
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        api_token: str | None = None,
        videogame_slugs: Sequence[str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        min_fetch_interval_s: float = 20.0,
        max_stale_on_error_s: float = 300.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token.strip() if isinstance(api_token, str) else None
        self._videogame_slugs = tuple(
            slug.strip().lower() for slug in (videogame_slugs or ()) if str(slug).strip()
        )
        self._timeout_s = max(0.1, float(timeout_s))
        self._min_fetch_interval_s = max(0.0, float(min_fetch_interval_s))
        self._max_stale_on_error_s = max(0.0, float(max_stale_on_error_s))
        self._now_provider = now_provider
        self._cached_snapshot: SportsLiveSnapshot | None = None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_s,
            headers=self._build_headers(),
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def enabled(self) -> bool:
        """缺 token 视为未启用，调用方需要据此屏蔽 provider。"""

        return bool(self._api_token)

    async def list_events(self) -> SportsLiveSnapshot:
        """拉取当前所有进行中的电竞比赛。"""

        observed_at = utc_now(self._now_provider)
        if not self.enabled:
            return _snapshot_with_status(
                SportsLiveSnapshot(source="pandascore", observed_at=observed_at, events=()),
                health=SportsLiveSourceHealth.FAILED,
                success=False,
                last_error="pandascore_token_missing",
            )
        if self._is_cache_fresh(observed_at):
            return _snapshot_with_status(
                self._cached_snapshot or SportsLiveSnapshot(source="pandascore", observed_at=observed_at, events=()),
                health=SportsLiveSourceHealth.CACHED,
            )
        try:
            payload = await self._get_lives()
        except Exception as exc:
            if self._is_cache_usable_after_error(observed_at):
                return _snapshot_with_status(
                    self._cached_snapshot or SportsLiveSnapshot(source="pandascore", observed_at=observed_at, events=()),
                    health=SportsLiveSourceHealth.CACHED,
                )
            if isinstance(exc, SportsDataRateLimitError):
                snapshot = _snapshot_with_status(
                    SportsLiveSnapshot(source="pandascore", observed_at=observed_at, events=()),
                    health=SportsLiveSourceHealth.RATE_LIMITED,
                    success=False,
                    last_error=str(exc),
                )
                self._cached_snapshot = snapshot
                return snapshot
            raise
        events = parse_pandascore_lives_payload(
            payload,
            observed_at=observed_at,
            videogame_slug_filter=self._videogame_slugs,
        )
        snapshot = SportsLiveSnapshot(
            source="pandascore",
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

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "user-agent": "sports-tail-trader/0.1",
        }
        if self._api_token:
            headers["authorization"] = f"Bearer {self._api_token}"
        return headers

    def _is_cache_fresh(self, observed_at: datetime) -> bool:
        if self._cached_snapshot is None:
            return False
        return _snapshot_age_seconds(self._cached_snapshot, observed_at) < self._min_fetch_interval_s

    def _is_cache_usable_after_error(self, observed_at: datetime) -> bool:
        if self._cached_snapshot is None:
            return False
        return _snapshot_age_seconds(self._cached_snapshot, observed_at) <= self._max_stale_on_error_s

    async def _get_lives(self) -> list[Mapping[str, Any]] | Mapping[str, Any]:
        operation = "pandascore_lives"
        try:
            response = await self._client.get(_LIVES_PATH)
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        try:
            data = response.json()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        if isinstance(data, list):
            return data
        if isinstance(data, Mapping):
            return data
        return []


def parse_pandascore_lives_payload(
    payload: Any,
    *,
    observed_at: datetime | None = None,
    videogame_slug_filter: Sequence[str] = (),
) -> tuple[LiveEvent, ...]:
    """把 Pandascore live payload 转成 ``LiveEvent`` 序列。

    Pandascore 顶层 ``/lives`` 返回数组，每项含 ``match`` mapping；如果端点返回
    了 ``{"data": [...]}`` 结构，也兼容性解包。
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    if isinstance(payload, Mapping):
        candidates: list[Any] = list(payload.get("data") or [])
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        candidates = list(payload)
    else:
        return ()
    events: list[LiveEvent] = []
    filter_set = {slug.strip().lower() for slug in videogame_slug_filter if str(slug).strip()}
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        match_payload = item.get("match") if isinstance(item.get("match"), Mapping) else item
        event = _parse_match(match_payload, observed_at=observed_at)
        if event is None:
            continue
        if filter_set and event.league.lower() not in filter_set:
            continue
        events.append(event)
    return tuple(events)


def _parse_match(match: Mapping[str, Any], *, observed_at: datetime) -> LiveEvent | None:
    opponents = match.get("opponents")
    if not isinstance(opponents, Sequence) or len(opponents) < 2:
        return None
    home_payload = _opponent_payload(opponents[0])
    away_payload = _opponent_payload(opponents[1])
    if home_payload is None or away_payload is None:
        return None
    results = match.get("results")
    home_id = home_payload.get("id")
    away_id = away_payload.get("id")
    home_score = _result_score_for(results, home_id)
    away_score = _result_score_for(results, away_id)
    home = Participant(
        role="home",
        name=str(home_payload.get("name") or "home"),
        score=home_score,
        display_name=first_text(home_payload, "name"),
        abbreviation=first_text(home_payload, "acronym"),
        short_name=first_text(home_payload, "acronym"),
        aliases=tuple(value for value in (first_text(home_payload, "slug"),) if value),
        external_ids=({"pandascore": str(home_id)} if home_id is not None else {}),
    )
    away = Participant(
        role="away",
        name=str(away_payload.get("name") or "away"),
        score=away_score,
        display_name=first_text(away_payload, "name"),
        abbreviation=first_text(away_payload, "acronym"),
        short_name=first_text(away_payload, "acronym"),
        aliases=tuple(value for value in (first_text(away_payload, "slug"),) if value),
        external_ids=({"pandascore": str(away_id)} if away_id is not None else {}),
    )
    videogame_slug = _videogame_slug(match)
    league = videogame_slug.upper() if videogame_slug else "ESPORTS"
    status_raw = str(match.get("status") or "").strip().lower()
    status = _map_match_status(status_raw)
    games = match.get("games")
    games_seq: Sequence[Any] = games if isinstance(games, Sequence) and not isinstance(games, (str, bytes)) else ()
    esports_state = EsportsGameState(
        best_of=int_value(match.get("number_of_games")),
        current_map_index=_current_map_index(games_seq),
        home_maps_won=home_score,
        away_maps_won=away_score,
        home_current_map_score=_current_map_score(games_seq, home_id),
        away_current_map_score=_current_map_score(games_seq, away_id),
        map_winners=_map_winners(games_seq, home_id, away_id),
    )
    raw_status = first_text(match, "status") or status_raw or ""
    period_label = _period_label(games_seq, status)
    match_id = match.get("id")
    source_event_id = str(match_id or match.get("slug") or "")
    event_start_time = _parse_iso_datetime(match.get("begin_at"))
    external_ids: dict[str, str] = {}
    if match_id is not None:
        external_ids["pandascore"] = str(match_id)
    return LiveEvent(
        source="pandascore",
        source_event_id=source_event_id,
        kind=LiveEventKind.TEAM_MATCH,
        league=league,
        sport="esports",
        participants=(home, away),
        status=status,
        period=period_label,
        seconds_remaining=None,
        observed_at=observed_at,
        event_start_time=event_start_time,
        event_name=str(match.get("name") or _tournament_name(match) or ""),
        external_ids=external_ids,
        raw_status=raw_status,
        esports_state=esports_state,
        source_payload={
            "sport": "esports",
            "videogame": videogame_slug,
            "slug": match.get("slug"),
            "tournament": _tournament_name(match),
            "begin_at": match.get("begin_at"),
            "start_timestamp": _begin_timestamp(match.get("begin_at")),
        },
    )


def _opponent_payload(opponent: Any) -> Mapping[str, Any] | None:
    if not isinstance(opponent, Mapping):
        return None
    payload = opponent.get("opponent")
    if isinstance(payload, Mapping):
        return payload
    return opponent


def _result_score_for(results: Any, opponent_id: Any) -> int:
    if opponent_id is None or not isinstance(results, Sequence):
        return 0
    for entry in results:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("team_id") == opponent_id:
            return int_value(entry.get("score")) or 0
    return 0


def _videogame_slug(match: Mapping[str, Any]) -> str | None:
    videogame = match.get("videogame")
    if isinstance(videogame, Mapping):
        return first_text(videogame, "slug", "name")
    return first_text(match, "videogame_title")


def _map_match_status(raw: str) -> SportsLiveGameStatus:
    text = raw.lower()
    if text in {"finished", "completed"}:
        return SportsLiveGameStatus.ENDED
    if text in {"running", "live"}:
        return SportsLiveGameStatus.LIVE
    if text == "not_started":
        return SportsLiveGameStatus.SCHEDULED
    if text == "postponed":
        return SportsLiveGameStatus.POSTPONED
    if text == "canceled" or text == "cancelled":
        return SportsLiveGameStatus.CANCELLED
    return SportsLiveGameStatus.UNKNOWN


def _current_map_index(games: Sequence[Any]) -> int | None:
    in_progress = [game for game in games if isinstance(game, Mapping) and str(game.get("status") or "").lower() == "running"]
    if in_progress:
        first = in_progress[0]
        position = int_value(first.get("position"))
        if position is not None:
            return position
    finished_positions = [
        int_value(game.get("position"))
        for game in games
        if isinstance(game, Mapping) and str(game.get("status") or "").lower() == "finished"
    ]
    finished = [pos for pos in finished_positions if pos is not None]
    return (max(finished) + 1) if finished else None


def _current_map_score(games: Sequence[Any], team_id: Any) -> int | None:
    if team_id is None:
        return None
    for game in games:
        if not isinstance(game, Mapping):
            continue
        if str(game.get("status") or "").lower() != "running":
            continue
        scores = game.get("scores") if isinstance(game.get("scores"), Sequence) else ()
        for entry in scores:
            if isinstance(entry, Mapping) and entry.get("team_id") == team_id:
                return int_value(entry.get("score"))
    return None


def _map_winners(games: Sequence[Any], home_id: Any, away_id: Any) -> tuple[str, ...]:
    winners: list[str] = []
    for game in games:
        if not isinstance(game, Mapping):
            continue
        if str(game.get("status") or "").lower() != "finished":
            continue
        winner = game.get("winner")
        winner_id = winner.get("id") if isinstance(winner, Mapping) else None
        if winner_id == home_id:
            winners.append("home")
        elif winner_id == away_id:
            winners.append("away")
    return tuple(winners)


def _period_label(games: Sequence[Any], status: SportsLiveGameStatus) -> str:
    if status == SportsLiveGameStatus.LIVE:
        index = _current_map_index(games) or 1
        return f"MAP{index}"
    return ""


def _tournament_name(match: Mapping[str, Any]) -> str | None:
    tournament = match.get("tournament")
    if isinstance(tournament, Mapping):
        return first_text(tournament, "name", "slug")
    return None


def _begin_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _snapshot_with_status(
    snapshot: SportsLiveSnapshot,
    *,
    health: SportsLiveSourceHealth,
    success: bool = True,
    last_error: str | None = None,
) -> SportsLiveSnapshot:
    return SportsLiveSnapshot(
        source="pandascore",
        observed_at=snapshot.observed_at,
        events=snapshot.events,
        source_statuses=(
            SportsLiveSourceStatus(
                source="pandascore",
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


__all__ = ["PandascoreLiveClient", "parse_pandascore_lives_payload"]
