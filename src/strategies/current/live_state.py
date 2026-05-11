"""当前体育扫尾策略的外部直播状态映射。

本模块只处理策略消费的 metadata 形态和 market 文本匹配语义。框架 worker
只调用这些纯函数，不在运行时层硬编码当前策略阈值或盘口判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
import unicodedata
from typing import Any

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import SportsLiveGame, SportsLiveGameStatus
from polymarket_trader.extension_api.live_state import LiveStateMatch

_GENERIC_ALIAS_TOKENS = {
    "a",
    "an",
    "and",
    "at",
    "basket",
    "bc",
    "bk",
    "cd",
    "club",
    "csd",
    "dep",
    "deportivo",
    "fc",
    "game",
    "hkk",
    "kk",
    "match",
    "market",
    "men",
    "office",
    "sc",
    "shoes",
    "sk",
    "sport",
    "sports",
    "team",
    "the",
    "to",
    "vs",
    "v",
    "women",
}
_TEAM_EVENT_START_TOLERANCE = timedelta(hours=3)
_TENNIS_EVENT_START_TOLERANCE = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class LiveMarketMatch:
    """外部比赛与 Polymarket market 的文本匹配结果。"""

    market: Market
    game: SportsLiveGame
    score: int
    matched_home_alias: str
    matched_away_alias: str

    def metadata(self) -> dict[str, Any]:
        """返回当前策略读取的入场 metadata。"""

        return {
            "live_game": live_game_metadata(self.game),
            "live_match": {
                "source": self.game.source,
                "source_event_id": self.game.source_event_id,
                "score": self.score,
                "matched_home_alias": self.matched_home_alias,
                "matched_away_alias": self.matched_away_alias,
            },
        }


def live_game_metadata(game: SportsLiveGame) -> dict[str, Any]:
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
        "source_conflicts": game.source_payload.get("source_conflicts", ()),
        "baseball_state": None if game.baseball_state is None else {
            "current_inning": game.baseball_state.current_inning,
            "inning_half": game.baseball_state.inning_half,
            "outs": game.baseball_state.outs,
            "offense_team": game.baseball_state.offense_team,
            "defense_team": game.baseball_state.defense_team,
            "occupied_bases": game.baseball_state.occupied_bases,
        },
        "tennis_state": _tennis_state_metadata(game.tennis_state),
        "soccer_state": None if game.soccer_state is None else {
            "period": game.soccer_state.period,
            "clock_minutes": game.soccer_state.clock_minutes,
            "added_minutes": game.soccer_state.added_minutes,
            "home_red_cards": game.soccer_state.home_red_cards,
            "away_red_cards": game.soccer_state.away_red_cards,
        },
        "esports_state": None if game.esports_state is None else {
            "best_of": game.esports_state.best_of,
            "current_map_index": game.esports_state.current_map_index,
            "home_maps_won": game.esports_state.home_maps_won,
            "away_maps_won": game.esports_state.away_maps_won,
            "home_current_map_score": game.esports_state.home_current_map_score,
            "away_current_map_score": game.esports_state.away_current_map_score,
        },
        "cricket_state": None if game.cricket_state is None else {
            "current_innings": game.cricket_state.current_innings,
            "batting_side": game.cricket_state.batting_side,
            "runs": game.cricket_state.runs,
            "wickets": game.cricket_state.wickets,
            "overs_completed": game.cricket_state.overs_completed,
            "target": game.cricket_state.target,
            "required_runs": game.cricket_state.required_runs,
            "required_balls": game.cricket_state.required_balls,
        },
    }


def match_live_game(market: Market, game: SportsLiveGame) -> LiveMarketMatch | None:
    """按队伍别名把一个外部比赛匹配到一个本地 market。

    这里不判断是否值得交易，只解决“这个比分属于哪个 market”的业务语义。
    """

    market_text = _market_text(market)
    return _match_live_game_from_market_text(market, game, market_text)


def _match_live_game_from_market_text(
    market: Market,
    game: SportsLiveGame,
    market_text: str,
) -> LiveMarketMatch | None:
    """使用已归一化 market 文本匹配单场比赛，避免批量匹配重复做文本清洗。"""

    market_start = _market_event_start_time(market)
    game_start = _game_event_start_time(game)
    precise_start_matched = False
    if (
        market_start is not None
        and game_start is not None
        and not _allows_event_start_time_match(game, market_start, game_start)
    ):
        return None
    if market_start is not None and game_start is not None:
        precise_start_matched = True
    market_date = _market_event_date(market_text)
    game_date = _game_event_date(game)
    if (
        not precise_start_matched
        and market_date is not None
        and game_date is not None
        and market_date != game_date
        and not _allows_adjacent_event_date(game, market_date, game_date)
    ):
        return None
    market_tokens = set(market_text.split())
    home_alias = _best_alias(market_text, market_tokens, game.home.match_aliases())
    away_alias = _best_alias(market_text, market_tokens, game.away.match_aliases())
    if home_alias is None or away_alias is None:
        return None
    home_score = _alias_score(home_alias)
    away_score = _alias_score(away_alias)
    return LiveMarketMatch(
        market=market,
        game=game,
        score=home_score + away_score,
        matched_home_alias=home_alias,
        matched_away_alias=away_alias,
    )


def best_live_match(
    market: Market,
    games: tuple[SportsLiveGame, ...],
) -> LiveMarketMatch | None:
    """返回 market 在当前比赛集合中的最高置信匹配。"""

    market_text = _market_text(market)
    matches = [
        match
        for game in games
        if (match := _match_live_game_from_market_text(market, game, market_text)) is not None
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: item.score)


def build_live_state_match(
    market: Market,
    games: tuple[SportsLiveGame, ...],
    *,
    market_end_horizon_seconds: int,
    bypass_resolver: "callable | None" = None,
) -> LiveStateMatch | None:
    """框架 hook ``match_live_state`` 的策略侧实现：返回强类型 LiveStateMatch。

    ``payload`` 由 ``LiveMarketMatch.metadata()`` 给出（含游戏快照 + match 信息），
    framework 不解释字段语义，admin/UI 可整体透传。
    ``signal_allowed/reason`` 由 ``entry_signal_gate`` 决定，可被 bypass_resolver
    在 ``market_end_too_far`` 情况下放行。
    """

    match = best_live_match(market, games)
    if match is None:
        return None
    matched_market = match.market
    game = match.game
    signal_allowed, signal_reason = entry_signal_gate(
        matched_market,
        game,
        market_end_horizon_seconds=market_end_horizon_seconds,
    )
    if not signal_allowed and signal_reason == "market_end_too_far" and bypass_resolver is not None:
        bypass = bypass_resolver(matched_market, game)
        if bypass is not None:
            signal_allowed = True
            signal_reason = bypass
    payload = match.metadata()
    return LiveStateMatch(
        market=matched_market,
        game=game,
        signal_allowed=signal_allowed,
        signal_reason=signal_reason,
        phase=str(getattr(game.status, "value", game.status) or "").strip().lower(),
        payload=payload,
    )


def entry_signal_gate(
    market: Market,
    game: SportsLiveGame,
    *,
    market_end_horizon_seconds: int,
) -> tuple[bool, str]:
    """判断直播匹配是否应触发 P0 入场信号。

    metadata 写入用于候选展示和复盘；P0 entry signal 只给真正进入扫尾观察窗、
    或已经结束但 Polymarket 尚未封盘的 market，避免远期 live 匹配挤压交易队列。
    """

    if game.status == SportsLiveGameStatus.ENDED:
        return True, "ended_not_closed"
    if game.status != SportsLiveGameStatus.LIVE:
        return False, f"sports_live_state_{game.status.value}"
    if market.end_date is None or market_end_horizon_seconds <= 0:
        return True, "market_end_unknown"
    current_time = _ensure_utc(game.observed_at) or datetime.now(timezone.utc)
    market_end = _ensure_utc(market.end_date)
    if market_end is None:
        return True, "market_end_unknown"
    seconds_until_end = (market_end - current_time).total_seconds()
    if seconds_until_end > market_end_horizon_seconds:
        return False, "market_end_too_far"
    return True, "within_tail_window"


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


def _tennis_state_metadata(state: Any) -> dict[str, Any] | None:
    """把强类型 TennisGameState 投影成策略稳定 metadata。"""

    if state is None:
        return None
    return {
        "home_sets_won": state.home_sets_won,
        "away_sets_won": state.away_sets_won,
        "current_set": state.current_set,
        "home_current_set_games": state.home_current_set_games,
        "away_current_set_games": state.away_current_set_games,
        "home_total_games": state.home_total_games,
        "away_total_games": state.away_total_games,
        "total_games": state.total_games,
        "set_scores": state.set_scores,
        "home_point": state.home_point,
        "away_point": state.away_point,
        "first_to_serve": state.first_to_serve,
        "serving_side": state.serving_side,
    }


def _market_event_date(market_text: str) -> date | None:
    match = re.search(r"(?<!\d)(20\d{2})\s+([01]\d)\s+([0-3]\d)(?!\d)", market_text)
    if match is None:
        return None
    return _date_from_parts(match.group(1), match.group(2), match.group(3))


def _market_event_start_time(market: Market) -> datetime | None:
    """返回 Polymarket 标注的比赛真实开赛时间，而不是 resolution/endDate。"""

    return _ensure_utc(market.game_start_time)


def _game_event_start_time(game: SportsLiveGame) -> datetime | None:
    for key in (
        "start_timestamp",
        "start_time_utc",
        "game_time_utc",
        "game_date",
        "date",
    ):
        parsed = _parse_event_datetime_value(game.source_payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _game_event_date(game: SportsLiveGame) -> date | None:
    for key in (
        "start_time_utc",
        "game_time_utc",
        "game_date",
        "official_date",
        "start_timestamp",
    ):
        parsed = _parse_event_date_value(game.source_payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _allows_event_start_time_match(
    game: SportsLiveGame,
    market_start: datetime,
    game_start: datetime,
) -> bool:
    """用精确开赛时间防止同队多场比赛串场。

    团队联赛常有同队连续多日比赛，不能只靠队名或日期匹配。网球允许较宽的
    赛程漂移窗口，因为资格赛/小赛会的页面时间和直播源时间可能被重排。
    """

    tolerance = (
        _TENNIS_EVENT_START_TOLERANCE
        if str(game.source_payload.get("sport") or "").strip().lower() == "tennis"
        else _TEAM_EVENT_START_TOLERANCE
    )
    return abs(market_start - game_start) <= tolerance


def _allows_adjacent_event_date(game: SportsLiveGame, market_date: date, game_date: date) -> bool:
    """处理网球跨时区开赛日期。

    Polymarket 网球 slug 常按页面本地日期命名，SofaScore 使用 UTC 开赛时间；
    同一场可能相差一天。团队联赛不使用该宽松规则，避免 MLB/NBA 同队多日赛串场。
    """

    if str(game.source_payload.get("sport") or "").strip().lower() != "tennis":
        return False
    return abs((market_date - game_date).days) <= 1


def _parse_event_datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(_timestamp_seconds(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        try:
            return datetime.fromtimestamp(_timestamp_seconds(float(text)), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    # 纯日期只能用于日期兜底，不能伪装成精确开赛时间参与硬匹配。
    if re.fullmatch(r"20\d{2}-[01]\d-[0-3]\d", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _parse_event_date_value(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(?<!\d)(20\d{2})-([01]\d)-([0-3]\d)(?!\d)", text)
    if match is not None:
        return _date_from_parts(match.group(1), match.group(2), match.group(3))
    match = re.search(r"(?<!\d)(20\d{2})([01]\d)([0-3]\d)(?!\d)", text)
    if match is not None:
        return _date_from_parts(match.group(1), match.group(2), match.group(3))
    return None


def _timestamp_seconds(value: int | float) -> float:
    timestamp = float(value)
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return timestamp


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _date_from_parts(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _best_alias(
    market_text: str,
    market_tokens: set[str],
    aliases: tuple[str, ...],
) -> str | None:
    candidates: list[tuple[int, str]] = []
    market_compact = _compact_text(market_text)
    for alias in aliases:
        seen_variants: set[str] = set()
        for normalized in _alias_text_variants(alias):
            if not normalized or normalized in seen_variants:
                continue
            seen_variants.add(normalized)
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
                continue
            if _compact_alias_matches_market(tokens, market_compact):
                candidates.append((_alias_score(phrase) - 2, alias))
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
    text = _fold_ascii(value.lower().replace("&", " and "))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _fold_ascii(value: str) -> str:
    """去除直播源球队名中的重音符号，保持跨来源队名匹配稳定。"""

    normalized = unicodedata.normalize("NFKD", value)
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _alias_text_variants(alias: str) -> tuple[str, ...]:
    """返回外部队名常见语言/拼写变体，用于跨来源匹配。

    Polymarket 和比分源经常混用英语、法语或连写队名；这里仅生成保守的
    多词队名变体，避免把短缩写误匹配到无关市场。
    """

    normalized = _normalize_text(alias)
    if not normalized:
        return ()
    variants = [normalized]
    replaced = re.sub(r"\bolympique\b", "olympic", normalized)
    if replaced != normalized:
        variants.append(replaced)
    translated = re.sub(r"\bthree towns\b", "san zhen", normalized)
    translated = re.sub(r"\btiger\b", "hu", translated)
    if translated != normalized:
        variants.append(translated)
    moroccan = re.sub(r"\bel jadidi\b", "el jadida", normalized)
    moroccan = re.sub(r"\brenaissance zemamra\b", "rca zemamra", moroccan)
    if moroccan != normalized:
        variants.append(moroccan)
    german_basketball = re.sub(r"\bfraport skyliners frankfurt\b", "fraport skyliners", normalized)
    if german_basketball != normalized:
        variants.append(german_basketball)
    russian = re.sub(r"\bmoscow\b", "moskva", normalized)
    russian = re.sub(r"\bdynamo\b", "dinamo", russian)
    if russian != normalized:
        variants.append(russian)
    tahiti = re.sub(r"\btahiti\b", "french polynesia", normalized)
    if tahiti != normalized:
        variants.append(tahiti)
    return tuple(variants)


def _compact_alias_matches_market(tokens: tuple[str, ...], market_compact: str) -> bool:
    if len(tokens) < 2:
        return False
    compact_alias = "".join(tokens)
    if len(compact_alias) < 8:
        return False
    return compact_alias in market_compact


def _compact_text(value: str) -> str:
    return value.replace(" ", "")
