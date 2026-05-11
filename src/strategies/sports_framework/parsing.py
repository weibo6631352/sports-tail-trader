"""把外部 metadata（直播源、运行时入参）解析成策略侧 ``LiveGameState``。

``baseball_state`` 直接构造 ``polymarket_trader.domain.sports_live.BaseballGameState``，
与 infra 归一化保持单一类型源。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from polymarket_trader.domain.sports_live import BaseballGameState

from .types import LiveGameState, LiveGameStatus, TennisGameState


def live_game_state_from_metadata(metadata: Mapping[str, Any]) -> LiveGameState | None:
    """从策略上下文 metadata 中读取直播比赛状态。

    支持两种形态：
    - ``metadata["live_game"]`` 是映射对象；
    - 直接在 metadata 顶层提供 ``league/home_score/away_score/status`` 等字段。
    """

    raw_game = metadata.get("live_game")
    if raw_game is None:
        raw_game = metadata
    if not isinstance(raw_game, Mapping):
        return None

    try:
        home_score = int(raw_game["home_score"])
        away_score = int(raw_game["away_score"])
    except (KeyError, TypeError, ValueError):
        return None

    observed_at = _datetime_value(raw_game.get("observed_at"))
    return LiveGameState(
        league=str(raw_game.get("league") or ""),
        home_name=str(raw_game.get("home_name") or "home"),
        away_name=str(raw_game.get("away_name") or "away"),
        home_score=home_score,
        away_score=away_score,
        period=str(raw_game.get("period") or ""),
        status=_game_status(raw_game.get("status")),
        seconds_remaining=_optional_int(raw_game.get("seconds_remaining")),
        observed_at=observed_at,
        source_conflicts=_source_conflicts(raw_game.get("source_conflicts")),
        baseball_state=_baseball_state(raw_game.get("baseball_state")),
        tennis_state=_tennis_state(raw_game.get("tennis_state")),
    )


def _game_status(value: object) -> LiveGameStatus:
    if isinstance(value, LiveGameStatus):
        return value
    normalized = str(value or "").strip().lower()
    for status in LiveGameStatus:
        if normalized == status.value:
            return status
    return LiveGameStatus.UNKNOWN


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _source_conflicts(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _baseball_state(value: object) -> BaseballGameState | None:
    if isinstance(value, BaseballGameState):
        return value
    if not isinstance(value, Mapping):
        return None
    occupied = value.get("occupied_bases")
    occupied_bases = tuple(
        base for base in (_optional_int(item) for item in occupied)
        if base is not None
    ) if isinstance(occupied, (tuple, list)) else ()
    return BaseballGameState(
        current_inning=_optional_int(value.get("current_inning")),
        inning_half=None if value.get("inning_half") is None else str(value.get("inning_half")).strip().lower(),
        outs=_optional_int(value.get("outs")),
        offense_team=None if value.get("offense_team") is None else str(value.get("offense_team")),
        defense_team=None if value.get("defense_team") is None else str(value.get("defense_team")),
        occupied_bases=occupied_bases,
    )


def _tennis_state(value: object) -> TennisGameState | None:
    if isinstance(value, TennisGameState):
        return value
    if not isinstance(value, Mapping):
        return None
    return TennisGameState(
        home_sets_won=_optional_int(value.get("home_sets_won")) or 0,
        away_sets_won=_optional_int(value.get("away_sets_won")) or 0,
        current_set=_optional_int(value.get("current_set")),
        home_current_set_games=_optional_int(value.get("home_current_set_games")),
        away_current_set_games=_optional_int(value.get("away_current_set_games")),
        home_total_games=_optional_int(value.get("home_total_games")) or 0,
        away_total_games=_optional_int(value.get("away_total_games")) or 0,
        set_scores=_tennis_set_scores(value.get("set_scores")),
        home_point=None if value.get("home_point") is None else str(value.get("home_point")),
        away_point=None if value.get("away_point") is None else str(value.get("away_point")),
        first_to_serve=None if value.get("first_to_serve") is None else str(value.get("first_to_serve")),
        serving_side=None if value.get("serving_side") is None else str(value.get("serving_side")),
    )


def _tennis_set_scores(value: object) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    scores: list[tuple[int, int]] = []
    for item in value:
        if isinstance(item, Mapping):
            home_games = _optional_int(item.get("home"))
            away_games = _optional_int(item.get("away"))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            home_games = _optional_int(item[0])
            away_games = _optional_int(item[1])
        else:
            continue
        if home_games is None or away_games is None:
            continue
        scores.append((home_games, away_games))
    return tuple(scores)


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
