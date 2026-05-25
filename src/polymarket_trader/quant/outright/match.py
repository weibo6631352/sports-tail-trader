"""把 MarketMetadataStore 中 season_odds_snapshot payload 还原成 SeasonOddsSnapshot。

``sports_season_odds_worker`` 写入的是 dict 形态（jsonable 序列化）。outright
评估器需要直接拿到强类型 SeasonOddsSnapshot 做反向定价。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from polymarket_trader.domain.sports_season import SeasonOddsSnapshot


def season_odds_from_metadata(metadata: Mapping[str, Any]) -> SeasonOddsSnapshot | None:
    """从策略 metadata 里取出 season_odds_snapshot 并解码。

    metadata 顶层可能直接含 ``season_odds_snapshot``，也可能在 ``live_game``
    或 ``snapshot`` 子键下；按预期路径取值，缺则返回 None。
    """

    raw = metadata.get("season_odds_snapshot") if isinstance(metadata, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    fair_payload = raw.get("fair_probabilities")
    if not isinstance(fair_payload, Mapping):
        return None
    fair: dict[str, Decimal] = {}
    for key, value in fair_payload.items():
        prob = _to_decimal(value)
        if prob is None:
            continue
        fair[str(key)] = prob
    if not fair:
        return None
    observed_at = _to_datetime(raw.get("observed_at"))
    if observed_at is None:
        return None
    return SeasonOddsSnapshot(
        market_key=str(raw.get("market_key") or ""),
        fair_probabilities=fair,
        observed_at=observed_at,
        source=str(raw.get("source") or "unknown"),
        source_event_id=raw.get("source_event_id"),
        source_conflicts=bool(raw.get("source_conflicts") or False),
        raw_payload=raw.get("raw_payload") if isinstance(raw.get("raw_payload"), Mapping) else {},
    )


def match_season_state(metadata: Mapping[str, Any]) -> SeasonOddsSnapshot | None:
    """match_live_state 在 outright family 的替代。

    给 strategy.py 的 match_live_state hook：当 descriptor.market_family
    == OUTRIGHT 时调用这里，返回 snapshot 用作 framework metadata 入口；
    没有 snapshot 时 framework 不会发 entry_signal。
    """

    return season_odds_from_metadata(metadata)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
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


__all__ = ["match_season_state", "season_odds_from_metadata"]
