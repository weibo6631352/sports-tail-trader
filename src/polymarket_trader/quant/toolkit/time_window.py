"""市场结算时间窗判断工具。

framework 不解释市场具体含义，但常见的"赛前 / 赛中 / 赛后 / 临近结算 / 结算后"
窗口判断在多个策略里都能复用。该模块只提供基于 ``Market.end_date`` /
``Market.game_start_time`` 的纯函数判断。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum

from polymarket_trader.domain.market import Market


class GamePhase(StrEnum):
    PRE_GAME = "pre_game"
    IN_GAME = "in_game"
    POST_GAME = "post_game"
    SETTLED = "settled"
    UNKNOWN = "unknown"


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def time_to_end(market: Market, *, now: datetime | None = None) -> timedelta | None:
    """返回距 market.end_date 的剩余时长；end_date 缺失返回 None。"""

    end = _ensure_utc(market.end_date)
    if end is None:
        return None
    return end - (_ensure_utc(now) or datetime.now(timezone.utc))


def is_within_tail_window(market: Market, *, horizon: timedelta, now: datetime | None = None) -> bool:
    """判断当前是否进入"距结算 horizon 内"的窗口（扫尾窗口的通用语义）。"""

    delta = time_to_end(market, now=now)
    if delta is None:
        return False
    return timedelta(0) <= delta <= horizon


def classify_phase(
    market: Market,
    *,
    now: datetime | None = None,
    pre_game_horizon: timedelta = timedelta(hours=2),
) -> GamePhase:
    """按比赛开始时间 + 结算时间分类窗口。

    - PRE_GAME: 距开赛 ≤ pre_game_horizon
    - IN_GAME: 已开赛 + 未到结算
    - POST_GAME: 已过结算时间 ≤ 6 小时（待清算）
    - SETTLED: 已过结算 > 6 小时
    - UNKNOWN: 缺少时间字段
    """

    current = _ensure_utc(now) or datetime.now(timezone.utc)
    start = _ensure_utc(market.game_start_time)
    end = _ensure_utc(market.end_date)
    if start is None and end is None:
        return GamePhase.UNKNOWN
    if start is not None and start - current > timedelta(0):
        return GamePhase.PRE_GAME if (start - current) <= pre_game_horizon else GamePhase.UNKNOWN
    if end is not None and current >= end:
        return GamePhase.POST_GAME if (current - end) <= timedelta(hours=6) else GamePhase.SETTLED
    return GamePhase.IN_GAME
