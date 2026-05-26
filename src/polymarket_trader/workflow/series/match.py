"""从 MarketMetadataStore 装载 SeriesState 与单场胜率 (p_per_game)。

``series_state_worker`` 写入 ``metadata["series_state"]``（jsonable 序列化的
SeriesState）；``game_odds_worker`` 写入 ``metadata["game_odds"]``。本模块负责
反序列化成强类型对象，让 evaluator 不直接读裸 dict。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from polymarket_trader.workflow.series.types import SeriesState


def series_state_from_metadata(metadata: Mapping[str, Any]) -> SeriesState | None:
    """metadata 顶层 ``series_state`` 键 → SeriesState；缺失/格式错误返回 None。

    元数据写入侧使用 ``polymarket_trader.serialization.jsonable``，时间会被
    序列化成 isoformat 字符串、Decimal/int 直接落 str/int，所以这里做反向解析。
    """

    raw = metadata.get("series_state") if isinstance(metadata, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    team_a = str(raw.get("team_a") or "").strip()
    team_b = str(raw.get("team_b") or "").strip()
    if not team_a or not team_b:
        return None
    wins_a = _int(raw.get("wins_a"))
    wins_b = _int(raw.get("wins_b"))
    best_of = _int(raw.get("best_of"))
    if wins_a is None or wins_b is None or best_of is None or best_of <= 0:
        return None
    observed_at = _datetime(raw.get("observed_at"))
    if observed_at is None:
        return None
    next_game_at = _datetime(raw.get("next_game_at"))
    return SeriesState(
        team_a=team_a,
        team_b=team_b,
        wins_a=wins_a,
        wins_b=wins_b,
        best_of=best_of,
        next_game_at=next_game_at,
        observed_at=observed_at,
    )


@dataclass(frozen=True, slots=True)
class GameOdds:
    """单场胜率快照——team_a 在下一场比赛中的获胜概率。"""

    team_a: str
    team_b: str
    p_a: Decimal
    observed_at: datetime
    source: str


def game_odds_from_metadata(metadata: Mapping[str, Any]) -> GameOdds | None:
    """metadata 顶层 ``game_odds`` 键 → GameOdds；缺失/格式错误返回 None。"""

    raw = metadata.get("game_odds") if isinstance(metadata, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    team_a = str(raw.get("team_a") or "").strip()
    team_b = str(raw.get("team_b") or "").strip()
    p_a = _decimal(raw.get("p_a"))
    observed_at = _datetime(raw.get("observed_at"))
    if not team_a or not team_b or p_a is None or observed_at is None:
        return None
    if p_a <= 0 or p_a >= 1:
        return None
    return GameOdds(
        team_a=team_a,
        team_b=team_b,
        p_a=p_a,
        observed_at=observed_at,
        source=str(raw.get("source") or "unknown"),
    )


@dataclass(frozen=True, slots=True)
class GameSpreads:
    """下一场让分快照——p_a_covers 是 home 在 spread_line 上覆盖的 de-vig 概率。"""

    team_a: str
    team_b: str
    spread_line: Decimal
    p_a_covers: Decimal
    observed_at: datetime
    source: str


def game_spreads_from_metadata(metadata: Mapping[str, Any]) -> GameSpreads | None:
    """metadata 顶层 ``game_spreads`` 键 → GameSpreads；缺失/格式错误返回 None。"""

    raw = metadata.get("game_spreads") if isinstance(metadata, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    team_a = str(raw.get("team_a") or "").strip()
    team_b = str(raw.get("team_b") or "").strip()
    spread_line = _decimal(raw.get("spread_line"))
    p_a = _decimal(raw.get("p_a_covers"))
    observed_at = _datetime(raw.get("observed_at"))
    if not team_a or not team_b or spread_line is None or p_a is None or observed_at is None:
        return None
    if p_a <= 0 or p_a >= 1:
        return None
    return GameSpreads(
        team_a=team_a,
        team_b=team_b,
        spread_line=spread_line,
        p_a_covers=p_a,
        observed_at=observed_at,
        source=str(raw.get("source") or "unknown"),
    )


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "GameOdds",
    "GameSpreads",
    "game_odds_from_metadata",
    "game_spreads_from_metadata",
    "series_state_from_metadata",
]
