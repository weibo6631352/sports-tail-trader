"""当前体育扫尾策略的远端发现查询构造。

远端 discovery 只能做粗筛；本文件负责把策略配置和直播源中的真实比赛，
转换成 Polymarket Gamma 支持的 ``DiscoveryQuery``。最终是否可交易仍由
universe、盘口解析、直播状态和风控决定。
"""

from __future__ import annotations

import re

from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveTeam,
)
from polymarket_trader.extension_api import DiscoveryQuery
from strategies.current.config import CurrentStrategyConfig

_LIVE_DISCOVERY_STATUSES = {
    SportsLiveGameStatus.SCHEDULED,
    SportsLiveGameStatus.LIVE,
    SportsLiveGameStatus.PAUSED,
    SportsLiveGameStatus.UNKNOWN,
}
_LIVE_DISCOVERY_STATUS_PRIORITY = {
    SportsLiveGameStatus.LIVE: 0,
    SportsLiveGameStatus.PAUSED: 1,
    SportsLiveGameStatus.SCHEDULED: 2,
    SportsLiveGameStatus.UNKNOWN: 3,
}
_LIVE_DISCOVERY_MAJOR_LEAGUE_TOKENS = (
    "nba",
    "nhl",
    "mlb",
    "wta",
    "atp",
)
_LIVE_DISCOVERY_SECONDARY_LEAGUE_TOKENS = (
    "nfl",
    "challenger",
)
_LIVE_DISCOVERY_LOW_COVERAGE_TOKENS = (
    "itf",
)
_TEAM_TERM_STOPWORDS = {
    "a",
    "an",
    "and",
    "club",
    "fc",
    "sc",
    "state",
    "team",
    "the",
    "united",
}


def build_configured_discovery_queries(config: CurrentStrategyConfig) -> tuple[DiscoveryQuery, ...]:
    """根据静态策略配置生成 Gamma 粗筛查询。"""

    title_searches = tuple(
        title_search.strip()
        for title_search in config.discovery_title_searches
        if title_search.strip()
    )
    tag_slugs = tuple(
        tag_slug.strip()
        for tag_slug in config.discovery_tag_slugs
        if tag_slug.strip()
    )
    if not tag_slugs:
        return tuple(DiscoveryQuery.title_search(title_search) for title_search in title_searches)
    if not title_searches:
        return tuple(
            DiscoveryQuery(
                name=f"tag_slug:{tag_slug}",
                params={"tag_slug": tag_slug},
            )
            for tag_slug in tag_slugs
        )
    return tuple(
        DiscoveryQuery(
            name=f"title_search:{title_search}|tag_slug:{tag_slug}",
            params={"title_search": title_search, "tag_slug": tag_slug},
        )
        for title_search in title_searches
        for tag_slug in tag_slugs
    )


def build_live_game_discovery_queries(
    config: CurrentStrategyConfig,
    games: tuple[SportsLiveGame, ...],
) -> tuple[DiscoveryQuery, ...]:
    """用直播源里的真实比赛生成高意图查询。

    Polymarket 的通用 ``nba/nhl/mlb`` 搜索经常优先返回冠军、选秀、系列赛或
    电竞等长期市场；直播比赛的队名搜索能更快扫到今日单场盘口。这里只生成
    远端粗筛词，不直接把任何 market 放入交易 universe。
    """

    tag_slugs = tuple(tag_slug.strip() for tag_slug in config.discovery_tag_slugs if tag_slug.strip())
    active_games = tuple(
        sorted(
            (game for game in games if game.status in _LIVE_DISCOVERY_STATUSES),
            key=_live_game_rank,
        )
    )
    queries: list[DiscoveryQuery] = []
    seen: set[tuple[str, str | None]] = set()
    for game in active_games[: config.sports_live_discovery_max_games]:
        for term in _game_query_terms(game):
            for tag_slug in tag_slugs or (None,):
                key = (term, tag_slug)
                if key in seen:
                    continue
                seen.add(key)
                params: dict[str, str] = {"title_search": term}
                suffix = term
                if tag_slug is not None:
                    params["tag_slug"] = tag_slug
                    suffix = f"{term}|tag_slug:{tag_slug}"
                queries.append(
                    DiscoveryQuery(
                        name=f"live_game:{game.league.lower()}:{game.source_event_id}:{suffix}",
                        params=params,
                    )
                )
                if len(queries) >= config.sports_live_discovery_max_queries:
                    return tuple(queries)
    return tuple(queries)


def _live_game_rank(game: SportsLiveGame) -> tuple[int, int, str, str]:
    """优先用真正 live 且 Polymarket 覆盖更高的比赛生成 discovery 查询。"""

    return (
        _LIVE_DISCOVERY_STATUS_PRIORITY.get(game.status, 99),
        _market_coverage_priority(game),
        game.league.lower(),
        game.source_event_id,
    )


def _market_coverage_priority(game: SportsLiveGame) -> int:
    """估计直播源比赛在 Polymarket 单场盘口中的发现价值。

    SofaScore 会返回大量 ITF 等低覆盖赛事；如果不按可交易覆盖排序，有限的
    Gamma 请求预算会先被低覆盖比赛消耗，导致 ATP/WTA 等真实可交易盘口延后。
    这里仍只影响 discovery 查询顺序，不改变最终入场判断。
    """

    league = game.league.lower()
    sport = str(game.source_payload.get("sport") or "").strip().lower()
    searchable_text = f"{league} {sport}"
    if any(token in searchable_text for token in _LIVE_DISCOVERY_MAJOR_LEAGUE_TOKENS):
        return 0
    if any(token in searchable_text for token in _LIVE_DISCOVERY_SECONDARY_LEAGUE_TOKENS):
        return 1
    if any(token in searchable_text for token in _LIVE_DISCOVERY_LOW_COVERAGE_TOKENS):
        return 4
    if sport == "tennis" or "tennis" in league:
        return 3
    return 2


def _game_query_terms(game: SportsLiveGame) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for team in (game.home, game.away):
        for term in _team_query_terms(team):
            if term in seen:
                continue
            seen.add(term)
            terms.append(term)
    return tuple(terms)


def _team_query_terms(team: SportsLiveTeam) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    location = _normalize_query_term(team.location or "")
    abbreviation = _normalize_query_term(team.abbreviation or "")
    aliases = tuple(
        alias
        for alias in (
            team.name,
            team.display_name,
            team.short_name,
            *team.aliases,
        )
        if alias
    )
    for alias in aliases:
        normalized = _normalize_query_term(alias)
        if not normalized or normalized == location or normalized == abbreviation:
            continue
        for term in _compact_team_terms(normalized):
            if term in seen:
                continue
            seen.add(term)
            terms.append(term)
    return tuple(terms)


def _compact_team_terms(text: str) -> tuple[str, ...]:
    parts = tuple(part for part in text.split() if part and part not in _TEAM_TERM_STOPWORDS)
    if not parts:
        return ()
    if len(parts) == 1:
        return (parts[0],)
    # 队名昵称通常是 Polymarket 单场 title_search 命中率最高的词，例如 Oilers、Wild、Lakers。
    return (parts[-1], " ".join(parts))


def _normalize_query_term(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", " ", value).strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    if len(normalized) < 3:
        return ""
    return normalized
