"""LiveSourceMatcher —— 候选事件粗筛 + 调用策略 match hook。

matcher 不做校验（团队名 / 时间 / league 验证由 `LiveSourceCalibrator` 后置
处理）。唯一职责：从 events 集合中找到与 market 关联的最佳候选 event，调
`match_hook` 拿到 `LiveStateMatch`（含 signal_allowed / phase / payload 等策略
层判定）。

# 复用策略 match hook

旧 SportsLiveStateWorker 已通过 `match_live_state` hook 解耦 framework 与策略——
团队名归一化 / race driver 匹配 / signal 判定都在 hook 内。新 matcher 同样复用
hook，避免重复实现匹配算法。`pipeline/ingest/live_source/calibrator.py` 在 matcher
之后做三角验证（matcher 内部已经做的"找到候选"是一回事，"校准这个候选可信"是
另一回事——拆开后 calibrator 可独立测试和升级）。
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
