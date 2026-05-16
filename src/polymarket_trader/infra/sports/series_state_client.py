"""ESPN scoreboard 系列赛热态适配器。

只服务 series WINNER 模型：返回 ``SeriesState``（team_a/b、wins_a/b、best_of、
next_game_at、observed_at）。Polymarket 系列赛市场在 NBA/NHL/MLB 季后赛期间
最常见——这三个联赛的 ESPN scoreboard payload 都把系列赛比分挂在
``competitions[0].series`` 节点上。

设计要点：
- 复用 ``espn_client.py`` 的 httpx async client / 错误归一化（``common`` 模块）。
- 默认 best_of：NBA / NHL 季后赛 7，MLB 季后赛 7（也支持 5）；从 payload
  ``series.totalCompetitions`` 解析时取实际值，缺失时按联赛默认。
- 一个稳定 ``series_key``：``"{league}:{home_id}-{away_id}"``——home/away
  顺序后续可能换，本 client 把 wins/team 按 home_id 是否对应 team_a 摆好；
  调用方拿 SeriesState 时 team_a/b 来自 ``home/away`` 字段。
- 错误处理：所有 HTTP / JSON 异常归一为 SportsDataClientError；fetch 网络错误
  返回 None（worker 据此发 MISSING_SERIES_STATE）；payload schema 异常记
  warning + None。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from polymarket_trader.infra.sports.common import (
    json_mapping_from_response,
    normalize_sports_data_error,
    utc_now,
)
from polymarket_trader.extension_api.live_state import SeriesState

logger = logging.getLogger(__name__)


_DEFAULT_BASE_URL = "https://site.api.espn.com"
_DEFAULT_LEAGUE_PATHS: dict[str, str] = {
    "nba": "/apis/site/v2/sports/basketball/nba/scoreboard",
    "nhl": "/apis/site/v2/sports/hockey/nhl/scoreboard",
    "mlb": "/apis/site/v2/sports/baseball/mlb/scoreboard",
}
# 缺 payload.totalCompetitions 时的默认 best_of。
_DEFAULT_BEST_OF: dict[str, int] = {"nba": 7, "nhl": 7, "mlb": 7}
# 当今日 scoreboard 找不到系列赛时，向前回溯的最大天数。
# 季后赛系列赛相邻两场间最多相差 2 天（主客场轮换），7 天足以覆盖所有休赛空档。
_SERIES_DATE_FALLBACK_DAYS = 7


class SeriesStateClient(Protocol):
    """系列赛热态的标准接口；strategy/worker 不耦合具体源。"""

    async def fetch(
        self,
        *,
        sport_key: str,
        series_key: str,
        observed_at: datetime | None = None,
    ) -> SeriesState | None: ...

    async def aclose(self) -> None: ...


class EspnSeriesStateClient:
    """ESPN scoreboard series 子节点 → SeriesState。

    ``sport_key`` 用 nba/nhl/mlb 三个小写联赛标识；``series_key`` 是 worker
    传入的稳定 key，本 client 用它在 events 列表里找匹配 series 节点
    （payload 里 series.id / event id 都可作为命中标识）。
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
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

    async def fetch(
        self,
        *,
        sport_key: str,
        series_key: str,
        observed_at: datetime | None = None,
    ) -> SeriesState | None:
        path = _DEFAULT_LEAGUE_PATHS.get(sport_key.lower())
        if path is None:
            logger.warning(
                "series_state_client.unsupported_sport",
                extra={"sport_key": sport_key},
            )
            return None
        observed_at = observed_at or utc_now(self._now_provider)
        operation = f"espn_series:{sport_key}"
        # 先查今天，找不到时向前回溯至多 _SERIES_DATE_FALLBACK_DAYS 天。
        # 季后赛相邻两场最多间隔 2 天，7 天足够覆盖所有休赛空档。
        today = observed_at.astimezone(timezone.utc).date()
        for day_offset in range(_SERIES_DATE_FALLBACK_DAYS + 1):
            params: dict[str, str] = {}
            if day_offset > 0:
                params["dates"] = (today - timedelta(days=day_offset)).strftime("%Y%m%d")
            try:
                response = await self._client.get(path, params=params)
                response.raise_for_status()
            except Exception as exc:
                raise normalize_sports_data_error(exc, operation=operation) from exc
            payload = json_mapping_from_response(response, operation=operation)
            try:
                state = parse_espn_scoreboard_series(
                    payload,
                    sport_key=sport_key,
                    series_key=series_key,
                    observed_at=observed_at,
                )
            except Exception:
                logger.warning(
                    "series_state.client_payload_invalid",
                    extra={"sport_key": sport_key, "series_key": series_key, "day_offset": day_offset},
                    exc_info=True,
                )
                return None
            if state is not None:
                return state
        return None


def parse_espn_scoreboard_series(
    payload: Mapping[str, Any],
    *,
    sport_key: str,
    series_key: str,
    observed_at: datetime,
) -> SeriesState | None:
    """ESPN scoreboard payload → SeriesState（命中 ``series_key`` 的事件）。

    匹配规则：归一化（lower）后 series_key 与以下任一字段相等
        - event.id
        - event.uid
        - event.shortName / event.name（去标点）
        - competition.id
        - series.title / series.summary
    """

    events = payload.get("events")
    if not isinstance(events, Sequence):
        return None
    norm_target = _normalize_match_key(series_key)
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if not _event_matches(event, norm_target):
            continue
        return _series_from_event(event, sport_key=sport_key, observed_at=observed_at)
    return None


def _event_matches(event: Mapping[str, Any], norm_target: str) -> bool:
    candidates: list[str] = []
    for key in ("id", "uid", "name", "shortName"):
        value = event.get(key)
        if value:
            candidates.append(str(value))
    # per_team_tokens[i] = set of normalized name tokens for competitor i
    per_team_tokens: list[set[str]] = []
    competitions = event.get("competitions")
    if isinstance(competitions, Sequence):
        for comp in competitions:
            if not isinstance(comp, Mapping):
                continue
            comp_id = comp.get("id")
            if comp_id:
                candidates.append(str(comp_id))
            series = comp.get("series")
            if isinstance(series, Mapping):
                for series_key_field in ("id", "title", "summary"):
                    value = series.get(series_key_field)
                    if value:
                        candidates.append(str(value))
            for comp_entry in comp.get("competitors") or ():
                if not isinstance(comp_entry, Mapping):
                    continue
                team = comp_entry.get("team")
                if not isinstance(team, Mapping):
                    continue
                tokens: set[str] = set()
                for field in ("abbreviation", "shortDisplayName", "displayName", "name"):
                    v = team.get(field)
                    if v:
                        tok = _normalize_match_key(str(v))
                        if tok:
                            tokens.add(tok)
                if tokens:
                    per_team_tokens.append(tokens)
    if any(_normalize_match_key(c) == norm_target for c in candidates):
        return True
    # Polymarket slugs embed team names (e.g. "avalanchevswild") but not ESPN
    # numeric event IDs.  Fall back: at least one name token from EACH competitor
    # must appear as a substring in the series_key.
    if len(per_team_tokens) >= 2 and all(
        any(t in norm_target for t in tokens) for tokens in per_team_tokens
    ):
        return True
    return False


def _series_from_event(
    event: Mapping[str, Any],
    *,
    sport_key: str,
    observed_at: datetime,
) -> SeriesState | None:
    competitions = event.get("competitions")
    if not isinstance(competitions, Sequence) or not competitions:
        return None
    competition = competitions[0]
    if not isinstance(competition, Mapping):
        return None
    series = competition.get("series")
    if not isinstance(series, Mapping):
        return None
    competitors = competition.get("competitors")
    if not isinstance(competitors, Sequence) or len(competitors) < 2:
        return None
    # ESPN payload 有两套 wins 来源：
    # 1. competition.series.competitors[].wins：系列赛专用，是最准确的来源。
    # 2. competition.competitors[].records[type=series/playoff].summary（"2-1"）：
    #    部分联赛/赛段没有此节点，不可依赖。
    # 先从 series.competitors 建立 team_id → wins 映射，供 _competitor_summary 查找。
    series_wins: dict[str, int] = {}
    for sc in (series.get("competitors") or []):
        if isinstance(sc, Mapping):
            tid = str(sc.get("id") or "").strip()
            w = sc.get("wins")
            if tid and w is not None:
                try:
                    series_wins[tid] = int(w)
                except (TypeError, ValueError):
                    pass
    # competitors 数组 ESPN 习惯第 0 个是 home / 第 1 个是 away；本模块按
    # 这个顺序映射 team_a = home，team_b = away。后续 evaluator 通过
    # team_resolver 反向匹配 outcome 文本，不依赖此顺序的语义。
    team_a_name, wins_a = _competitor_summary(competitors[0], series_wins=series_wins)
    team_b_name, wins_b = _competitor_summary(competitors[1], series_wins=series_wins)
    if not team_a_name or not team_b_name:
        return None
    best_of = _best_of(series, sport_key)
    next_game_at = _next_game_at(event)
    return SeriesState(
        team_a=team_a_name,
        team_b=team_b_name,
        wins_a=wins_a,
        wins_b=wins_b,
        best_of=best_of,
        next_game_at=next_game_at,
        observed_at=observed_at,
    )


def _competitor_summary(competitor: Any, *, series_wins: dict[str, int] | None = None) -> tuple[str, int]:
    if not isinstance(competitor, Mapping):
        return "", 0
    team = competitor.get("team")
    name = ""
    team_id = ""
    if isinstance(team, Mapping):
        team_id = str(team.get("id") or "").strip()
        for key in ("displayName", "name", "shortDisplayName", "abbreviation"):
            value = team.get(key)
            if value:
                name = str(value).strip()
                break
    # 优先从调用方传入的 series.competitors 映射取 wins（最权威）。
    if series_wins and team_id and team_id in series_wins:
        return name, series_wins[team_id]
    # 回退：竞争者自身 records 节点里类型为 series/playoff 的条目。
    wins = 0
    records = competitor.get("records")
    if isinstance(records, Sequence):
        for record in records:
            if not isinstance(record, Mapping):
                continue
            if str(record.get("type") or "").lower() in {"series", "playoff"}:
                wins = _wins_from_summary(record.get("summary"))
                if wins:
                    return name, wins
    return name, wins


def _wins_from_summary(summary: Any) -> int:
    if not summary:
        return 0
    text = str(summary).strip()
    if "-" in text:
        head = text.split("-", 1)[0].strip()
        try:
            return int(head)
        except ValueError:
            return 0
    return 0


def _best_of(series: Mapping[str, Any], sport_key: str) -> int:
    for key in ("totalCompetitions", "type"):
        value = series.get(key)
        if isinstance(value, int) and value > 0:
            return value
        try:
            parsed = int(str(value))
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            continue
    # series.competitors[].wins 合理上限也能反推 best_of：先看 series.summary。
    summary = series.get("summary")
    if isinstance(summary, str) and "best of" in summary.lower():
        for token in summary.split():
            if token.isdigit():
                value = int(token)
                if value > 0:
                    return value
    return _DEFAULT_BEST_OF.get(sport_key.lower(), 7)


def _next_game_at(event: Mapping[str, Any]) -> datetime | None:
    raw = event.get("date")
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _normalize_match_key(value: str) -> str:
    """归一化 series_key 与 payload 字段，宽容空白 / 标点差异。"""

    if not value:
        return ""
    out = []
    for ch in str(value).lower():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


__all__ = [
    "EspnSeriesStateClient",
    "SeriesStateClient",
    "parse_espn_scoreboard_series",
]
