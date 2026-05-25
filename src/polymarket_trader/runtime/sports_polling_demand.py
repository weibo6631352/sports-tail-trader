"""Demand-driven sports polling 的需求提供器。

GoalserveInplayClient / GoalserveLivescoreClient 在每轮轮询前调一次
``active_sports_provider()`` 拿到当前需要拉取的运动集合：

- 有 live / 即将开赛 / 时间未知但 end_date 在 6h 内的 tracked market → 加入
- 其他 sport → 整轮跳过 HTTP 抓取

这是 CLAUDE.md §0 "不订阅就不轮询" 原则的具体落地，把 5 req/s 稳态出站
压到 0.7 req/s 的关键改动之一。

两个 builder 都只做 ``registry.snapshot()`` 遍历 + 内存判断，不做 I/O；
每轮轮询都会调，必须保持廉价。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from polymarket_trader.infra.sports.goalserve_livescore_client import (
    SPORT_CODE_TO_FEED_KEYS,
)
from polymarket_trader.quant.live_state import _market_sport_codes
from polymarket_trader.runtime.registry import MarketRegistry


# MLB/NBA/NHL/NFL/Tennis 单场比赛持续时间上限 ≤ 6 小时，加 1h buffer 保证末段
# inplay 仍拉。Polymarket end_date 对体育单场市场常 == game_start_time（Gamma
# API 字段语义混淆），不能用 end > now 判 live。
_GAME_INPLAY_WINDOW = timedelta(hours=7)
_LIVESCORE_GAME_WINDOW = timedelta(hours=7)
_NEAR_START_WINDOW = timedelta(minutes=60)
_GAME_START_UNKNOWN_BUFFER = timedelta(hours=6)


def build_livescore_active_sports_provider(
    registry: MarketRegistry,
) -> Callable[[], frozenset[str]]:
    """Livescore demand-driven 轮询的 active_sports_provider。

    返回有需求的 ``SPORT_CODE_TO_FEED_KEYS`` key 集合。某市场 live（已开赛且
    未结束）或将在 60 分钟内开赛时，把它的运动码映射出的所有 feed key 纳入。
    """

    def _provider() -> frozenset[str]:
        now = datetime.now(timezone.utc)
        near_start_cutoff = now + _NEAR_START_WINDOW
        active: set[str] = set()
        for market in registry.snapshot().markets:
            start, end = _normalize_market_window(market.game_start_time, market.end_date)
            if not _is_market_active(
                start=start,
                end=end,
                now=now,
                near_start_cutoff=near_start_cutoff,
                game_window=_LIVESCORE_GAME_WINDOW,
            ):
                continue
            for code in _market_sport_codes(market):
                feed_keys = SPORT_CODE_TO_FEED_KEYS.get(code)
                if feed_keys:
                    active.update(feed_keys)
        return frozenset(active)

    return _provider


def build_inplay_active_sports_provider(
    registry: MarketRegistry,
) -> Callable[[], frozenset[str]]:
    """Inplay GZIP feed demand-driven 轮询的 active_sports_provider。

    与 livescore 同样遍历 tracked-market registry，但返回的是 ``_market_sport_codes``
    输出的**规范运动码**集合（football / basketball / ice-hockey 等）——
    GoalserveInplayClient 自己用 ``SPORT_CODE_TO_INPLAY_KEYS`` 把规范码映射到
    feed 路径 token。
    """

    def _provider() -> frozenset[str]:
        now = datetime.now(timezone.utc)
        near_start_cutoff = now + _NEAR_START_WINDOW
        active: set[str] = set()
        for market in registry.snapshot().markets:
            start, end = _normalize_market_window(market.game_start_time, market.end_date)
            if not _is_market_active(
                start=start,
                end=end,
                now=now,
                near_start_cutoff=near_start_cutoff,
                game_window=_GAME_INPLAY_WINDOW,
            ):
                continue
            active.update(_market_sport_codes(market))
        return frozenset(active)

    return _provider


def _normalize_market_window(
    start: datetime | None, end: datetime | None
) -> tuple[datetime | None, datetime | None]:
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start, end


def _is_market_active(
    *,
    start: datetime | None,
    end: datetime | None,
    now: datetime,
    near_start_cutoff: datetime,
    game_window: timedelta,
) -> bool:
    """三种 active 判定，命中任一即视为需要拉直播数据。

    - is_live：已开赛且仍在 game_window 内（默认 7h）
    - is_near_start：未来 60min 内开赛
    - start_unknown_active：game_start_time 缺失，但 end_date 在未来 6h 内
    """

    is_live = start is not None and start <= now <= start + game_window
    is_near_start = start is not None and now <= start <= near_start_cutoff
    start_unknown_active = start is None and (
        end is None or now < end < now + _GAME_START_UNKNOWN_BUFFER
    )
    return is_live or is_near_start or start_unknown_active
