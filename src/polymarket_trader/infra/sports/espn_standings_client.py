"""ESPN 联赛积分榜 REST 适配器。

不同于 ``espn_client``（scoreboard，秒级单场），这里读取 standings 接口，
是 outright 评估的赛季级输入。低频拉取（小时级），缓存策略由 worker 控制。

ESPN standings path 形如:
``/apis/v2/sports/{sport}/{league}/standings``
- basketball/nba, basketball/wnba
- hockey/nhl
- baseball/mlb
- football/nfl
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from polymarket_trader.domain.sports_season import (
    SeasonSnapshot,
    SeasonStandingRow,
    SeasonStandings,
)
from polymarket_trader.infra.sports.common import (
    SportsDataResponseError,
    first_text,
    int_value,
    json_mapping_from_response,
    normalize_sports_data_error,
    utc_now,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://site.api.espn.com"
_DEFAULT_LEAGUE_PATHS: dict[str, str] = {
    "nba": "/apis/v2/sports/basketball/nba/standings",
    "wnba": "/apis/v2/sports/basketball/wnba/standings",
    "nfl": "/apis/v2/sports/football/nfl/standings",
    "nhl": "/apis/v2/sports/hockey/nhl/standings",
    "mlb": "/apis/v2/sports/baseball/mlb/standings",
}


class EspnStandingsClient:
    """读取 ESPN standings 并归一化成 SeasonSnapshot。"""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        leagues: Sequence[str] = ("nba", "nhl", "nfl", "mlb"),
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._leagues = tuple(_normalize(league) for league in leagues if str(league).strip())
        self._now_provider = now_provider
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            headers={"accept": "application/json"},
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_snapshot(self) -> SeasonSnapshot:
        observed_at = utc_now(self._now_provider)
        standings_list: list[SeasonStandings] = []
        for league in self._leagues:
            try:
                payload = await self._get_standings(league)
            except Exception:
                logger.warning("espn_standings_client.fetch_failed", extra={"league": league}, exc_info=True)
                continue
            standings = parse_espn_standings_payload(
                payload,
                league=league,
                observed_at=observed_at,
            )
            if standings is not None:
                standings_list.append(standings)
        return SeasonSnapshot(
            observed_at=observed_at,
            standings=tuple(standings_list),
            series=(),
            source="espn",
        )

    async def _get_standings(self, league: str) -> Mapping[str, Any]:
        path = _DEFAULT_LEAGUE_PATHS.get(league)
        if path is None:
            raise SportsDataResponseError(
                f"unsupported standings league: {league}",
                operation="espn_standings",
                code="unsupported_league",
            )
        operation = f"espn_standings:{league}"
        try:
            response = await self._client.get(path)
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        return json_mapping_from_response(response, operation=operation)


def parse_espn_standings_payload(
    payload: Mapping[str, Any],
    *,
    league: str,
    observed_at: datetime | None = None,
) -> SeasonStandings | None:
    """把 ESPN standings 原始 payload 转成内部 SeasonStandings。

    ESPN payload 结构（简化）：
    ``{"season": {"year": 2026}, "children": [{"name": "Conf", "standings": {"entries": [...]}}, ...]}``
    或者直接 ``{"standings": {"entries": [...]}}``。
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    season_id = _season_id_from(payload)
    rows: list[SeasonStandingRow] = []
    for group in _iter_entry_groups(payload):
        conference = group.get("conference")
        division = group.get("division")
        for entry in group.get("entries", ()):
            row = _row_from_entry(entry, conference=conference, division=division)
            if row is not None:
                rows.append(row)
    if not rows:
        return None
    return SeasonStandings(
        league=league.upper(),
        season_id=season_id,
        observed_at=observed_at,
        rows=tuple(rows),
        source="espn",
    )


def _season_id_from(payload: Mapping[str, Any]) -> str:
    season = payload.get("season")
    if isinstance(season, Mapping):
        year = first_text(season, "year", "displayName")
        if year:
            return year
    return ""


def _iter_entry_groups(payload: Mapping[str, Any]):
    """yields ``{"conference": str|None, "division": str|None, "entries": [...]}``."""

    children = payload.get("children")
    if isinstance(children, Sequence) and children:
        for child in children:
            if not isinstance(child, Mapping):
                continue
            standings = child.get("standings")
            entries = standings.get("entries") if isinstance(standings, Mapping) else None
            if not isinstance(entries, Sequence):
                continue
            yield {
                "conference": first_text(child, "name", "shortName"),
                "division": None,
                "entries": entries,
            }
        return
    standings = payload.get("standings")
    if isinstance(standings, Mapping):
        entries = standings.get("entries")
        if isinstance(entries, Sequence):
            yield {"conference": None, "division": None, "entries": entries}


def _row_from_entry(
    entry: Any,
    *,
    conference: str | None,
    division: str | None,
) -> SeasonStandingRow | None:
    if not isinstance(entry, Mapping):
        return None
    team = entry.get("team")
    team_payload = team if isinstance(team, Mapping) else {}
    team_name = first_text(team_payload, "displayName", "name", "shortDisplayName", "abbreviation")
    if not team_name:
        return None
    stats = entry.get("stats") if isinstance(entry.get("stats"), Sequence) else ()
    stat_map = {str(item.get("name") or item.get("type") or "").lower(): item for item in stats if isinstance(item, Mapping)}
    wins = int_value(_stat_value(stat_map.get("wins"))) or 0
    losses = int_value(_stat_value(stat_map.get("losses"))) or 0
    ties = int_value(_stat_value(stat_map.get("ties"))) or 0
    win_pct = _decimal_value(_stat_value(stat_map.get("winpercent") or stat_map.get("winningpercentage")))
    games_back = _decimal_value(_stat_value(stat_map.get("gamesbehind")))
    seed = int_value(_stat_value(stat_map.get("playoffseed") or stat_map.get("seed")))
    clinched = first_text(entry, "clinchIndicator") or _clinched_from_stats(stat_map)
    return SeasonStandingRow(
        team=team_name,
        wins=wins,
        losses=losses,
        ties=ties,
        win_pct=win_pct,
        games_back=games_back,
        conference=conference,
        division=division,
        seed=seed,
        clinched=clinched,
    )


def _stat_value(stat: Any) -> Any:
    if not isinstance(stat, Mapping):
        return None
    return stat.get("value") if "value" in stat else stat.get("displayValue")


def _decimal_value(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _clinched_from_stats(stat_map: Mapping[str, Mapping[str, Any]]) -> str | None:
    for key in ("clinchindicator", "playoffstatus", "clinched"):
        stat = stat_map.get(key)
        text = first_text(stat, "displayValue", "summary") if isinstance(stat, Mapping) else None
        if text:
            return text
    return None


def _normalize(value: str) -> str:
    return str(value or "").strip().lower()


__all__ = ["EspnStandingsClient", "parse_espn_standings_payload"]
