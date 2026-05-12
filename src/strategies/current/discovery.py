"""当前体育扫尾策略的远端发现查询构造。

远端 discovery 只能做粗筛；本文件负责把策略配置和直播源中的真实比赛，
转换成 Polymarket Gamma 支持的 ``DiscoveryQuery``。最终是否可交易仍由
universe、盘口解析、直播状态和风控决定。
"""

from __future__ import annotations

import re

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
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


def build_live_event_discovery_queries(
    config: CurrentStrategyConfig,
    events: tuple[LiveEvent, ...],
) -> tuple[DiscoveryQuery, ...]:
    """用直播源里的真实比赛生成高意图查询。

    Polymarket 的通用 ``nba/nhl/mlb`` 搜索经常优先返回冠军、选秀、系列赛或
    电竞等长期市场；直播比赛的队名搜索能更快扫到今日单场盘口。这里只生成
    远端粗筛词，不直接把任何 market 放入交易 universe。
    """

    tag_slugs = tuple(tag_slug.strip() for tag_slug in config.discovery_tag_slugs if tag_slug.strip())
    active_events = tuple(
        sorted(
            (event for event in events if event.status in _LIVE_DISCOVERY_STATUSES),
            key=_live_event_rank,
        )
    )
    queries: list[DiscoveryQuery] = []
    seen: set[tuple[str, str | None]] = set()
    for event in active_events[: config.tail_live_discovery_max_games]:
        for term in _event_query_terms(event):
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
                        name=f"live_event:{event.league.lower()}:{event.source_event_id}:{suffix}",
                        params=params,
                    )
                )
                if len(queries) >= config.tail_live_discovery_max_queries:
                    return tuple(queries)
    return tuple(queries)


def _live_event_rank(event: LiveEvent) -> tuple[int, int, str, str]:
    """优先用真正 live 且 Polymarket 覆盖更高的比赛生成 discovery 查询。"""

    return (
        _LIVE_DISCOVERY_STATUS_PRIORITY.get(event.status, 99),
        _market_coverage_priority(event),
        event.league.lower(),
        event.source_event_id,
    )


def _market_coverage_priority(event: LiveEvent) -> int:
    """估计直播源事件在 Polymarket 单场盘口中的发现价值。

    SofaScore 会返回大量 ITF 等低覆盖赛事；如果不按可交易覆盖排序，有限的
    Gamma 请求预算会先被低覆盖比赛消耗，导致 ATP/WTA 等真实可交易盘口延后。
    这里仍只影响 discovery 查询顺序，不改变最终入场判断。
    """

    league = event.league.lower()
    sport = (event.sport or "").strip().lower()
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


def _event_query_terms(event: LiveEvent) -> tuple[str, ...]:
    """按 kind 分支构造查询词。

    team_match：home/away 对阵 + 单队名兜底；
    race：用 leader_driver / event_name 关键字，赛车 market 经常用赛事名 + 车手提问。
    """

    if event.kind == LiveEventKind.TEAM_MATCH and event.home is not None and event.away is not None:
        return _team_event_query_terms(event)
    if event.kind == LiveEventKind.RACE:
        return _race_event_query_terms(event)
    return ()


def _team_event_query_terms(event: LiveEvent) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for term in _matchup_query_terms(event):
        if term in seen:
            continue
        seen.add(term)
        terms.append(term)
    for participant in (event.home, event.away):
        if participant is None:
            continue
        for term in _team_query_terms(participant):
            if term in seen:
                continue
            seen.add(term)
            terms.append(term)
    return tuple(terms)


def _race_event_query_terms(event: LiveEvent) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    leader = event.race_state.leader_driver if event.race_state else None
    if leader:
        normalized = _normalize_query_term(leader)
        if normalized:
            for term in _compact_team_terms(normalized):
                if term in seen:
                    continue
                seen.add(term)
                terms.append(term)
    event_name = event.event_name
    if event_name:
        normalized = _normalize_query_term(event_name)
        if normalized and normalized not in seen:
            seen.add(normalized)
            terms.append(normalized)
    return tuple(terms)


def _matchup_query_terms(event: LiveEvent) -> tuple[str, ...]:
    """生成优先级最高的对阵组合词，减少单队名搜索带来的远期噪声。"""

    if event.home is None or event.away is None:
        return ()
    home_terms = _team_query_terms(event.home)
    away_terms = _team_query_terms(event.away)
    if not home_terms or not away_terms:
        return ()
    home = home_terms[0]
    away = away_terms[0]
    if home == away:
        return ()
    return (f"{home} {away}", f"{away} {home}")


def _team_query_terms(participant: Participant) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    location = _normalize_query_term(participant.location or "")
    abbreviation = _normalize_query_term(participant.abbreviation or "")
    aliases = tuple(
        alias
        for alias in (
            participant.name,
            participant.display_name,
            participant.short_name,
            *participant.aliases,
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
