"""LiveSourceMatcher —— 候选事件粗筛 + 调 match hook。

matcher 不做校验（团队名 / 时间 / league 验证由 ``LiveSourceCalibrator``
后置）。唯一职责：从 events 集合中找到 market 关联的最佳候选 event，调
``match_hook``（通常是 ``workflow.match_live_state``）拿到 ``LiveStateMatch``。

framework 不直接 import workflow——通过 callable 注入保持单向依赖。
"""

from __future__ import annotations

from collections.abc import Callable

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import LiveEvent, LiveStateMatch

MatchHook = Callable[[Market, tuple[LiveEvent, ...]], LiveStateMatch | None]


class LiveSourceMatcher:
    def __init__(self, *, match_hook: MatchHook) -> None:
        self._hook = match_hook

    def match(
        self,
        market: Market,
        events: tuple[LiveEvent, ...],
    ) -> LiveStateMatch | None:
        if not events:
            return None
        return self._hook(market, events)
