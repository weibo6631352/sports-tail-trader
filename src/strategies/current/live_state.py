"""当前体育扫尾策略的外部直播状态映射。

本模块只处理策略消费的 metadata 形态和 market 文本匹配语义。框架 worker
只调用这些纯函数，不在运行时层硬编码当前策略阈值或盘口判断。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import SportsLiveGame

_GENERIC_ALIAS_TOKENS = {
    "a",
    "an",
    "and",
    "at",
    "club",
    "fc",
    "game",
    "match",
    "market",
    "men",
    "team",
    "the",
    "to",
    "vs",
    "v",
    "women",
}


@dataclass(frozen=True, slots=True)
class SportsLiveMarketMatch:
    """外部比赛与 Polymarket market 的文本匹配结果。"""

    market: Market
    game: SportsLiveGame
    score: int
    matched_home_alias: str
    matched_away_alias: str

    def metadata(self) -> dict[str, Any]:
        """返回当前策略读取的入场 metadata。"""

        return {
            "sports_tail_game": sports_tail_game_metadata(self.game),
            "sports_live_match": {
                "source": self.game.source,
                "source_event_id": self.game.source_event_id,
                "score": self.score,
                "matched_home_alias": self.matched_home_alias,
                "matched_away_alias": self.matched_away_alias,
            },
        }


def sports_tail_game_metadata(game: SportsLiveGame) -> dict[str, Any]:
    """把通用直播比赛状态转换成体育扫尾策略的稳定 metadata。"""

    return {
        "league": game.league,
        "home_name": game.home.name,
        "away_name": game.away.name,
        "home_score": game.home.score,
        "away_score": game.away.score,
        "period": game.period,
        "seconds_remaining": game.seconds_remaining,
        "status": game.status.value,
        "observed_at": None if game.observed_at is None else game.observed_at.isoformat(),
        "source": game.source,
        "source_event_id": game.source_event_id,
        "raw_status": game.raw_status,
    }


def match_sports_live_game(market: Market, game: SportsLiveGame) -> SportsLiveMarketMatch | None:
    """按队伍别名把一个外部比赛匹配到一个本地 market。

    这里不判断是否值得交易，只解决“这个比分属于哪个 market”的业务语义。
    """

    market_text = _market_text(market)
    market_tokens = set(market_text.split())
    home_alias = _best_alias(market_text, market_tokens, game.home.match_aliases())
    away_alias = _best_alias(market_text, market_tokens, game.away.match_aliases())
    if home_alias is None or away_alias is None:
        return None
    home_score = _alias_score(home_alias)
    away_score = _alias_score(away_alias)
    return SportsLiveMarketMatch(
        market=market,
        game=game,
        score=home_score + away_score,
        matched_home_alias=home_alias,
        matched_away_alias=away_alias,
    )


def best_sports_live_match(
    market: Market,
    games: tuple[SportsLiveGame, ...],
) -> SportsLiveMarketMatch | None:
    """返回 market 在当前比赛集合中的最高置信匹配。"""

    matches = [match for game in games if (match := match_sports_live_game(market, game)) is not None]
    if not matches:
        return None
    return max(matches, key=lambda item: item.score)


def sports_live_metadata_match(
    market: Market,
    games: tuple[SportsLiveGame, ...],
) -> tuple[Market, SportsLiveGame, Mapping[str, Any]] | None:
    """返回运行时同步 worker 可写入 metadata store 的匹配结果。"""

    match = best_sports_live_match(market, games)
    if match is None:
        return None
    return match.market, match.game, match.metadata()


def _market_text(market: Market) -> str:
    return _normalize_text(
        " ".join(
            part
            for part in (
                market.market_question,
                market.market_name,
                market.market_slug,
                market.event_title,
                market.event_slug,
                market.category,
                " ".join(market.tags),
                " ".join(outcome.outcome for outcome in market.outcomes),
            )
            if part
        )
    )


def _best_alias(
    market_text: str,
    market_tokens: set[str],
    aliases: tuple[str, ...],
) -> str | None:
    candidates: list[tuple[int, str]] = []
    for alias in aliases:
        normalized = _normalize_text(alias)
        if not normalized:
            continue
        tokens = tuple(token for token in normalized.split() if token not in _GENERIC_ALIAS_TOKENS)
        if not tokens:
            continue
        if len(tokens) == 1:
            token = tokens[0]
            if len(token) < 3 and token not in market_tokens:
                continue
            if token in market_tokens:
                candidates.append((_alias_score(token), alias))
            continue
        phrase = " ".join(tokens)
        if f" {phrase} " in f" {market_text} ":
            candidates.append((_alias_score(phrase), alias))
            continue
        if all(token in market_tokens for token in tokens):
            candidates.append((_alias_score(phrase) - 1, alias))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _alias_score(alias: str) -> int:
    normalized = _normalize_text(alias)
    tokens = [token for token in normalized.split() if token not in _GENERIC_ALIAS_TOKENS]
    return sum(max(1, len(token)) for token in tokens)


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())
