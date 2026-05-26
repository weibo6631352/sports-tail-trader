"""SportsSubscriptionPolicy —— 决定 market 该订哪个 primary live source。

# 规则（原架构方案 §3.3）

- inplay 覆盖的 sport（参 `source.INPLAY_COVERED_SPORTS`）→ **只订 inplay**（prio 100）
- inplay 不覆盖的 sport → 只订 livescore（prio 50）
- 推不出 sport 的 market（无 category / tags 信号）→ 返回空，不订任何 source
- **每个 market 始终只订 1 个 primary live source**——inplay 临时 stale 的兜底
  靠 source 自身退避重试（client 内部 backoff），不靠订阅冗余

# sport_resolver

policy 本身不内置运动识别——sport_resolver 由策略层注入（处理 market.category /
market.tags / market.sports_market_type / slug 关键词等多源信号融合）。这样运动
识别逻辑随策略演化，不污染 framework。
"""

from __future__ import annotations

from collections.abc import Callable

from polymarket_trader.domain.market import Market

from .source import INPLAY_COVERED_SPORTS, LiveSourceKey, LiveSourceProvider

SportResolver = Callable[[Market], str | None]
ActivePredicate = Callable[[Market], bool]


def _always_active(_: Market) -> bool:
    return True


class SportsSubscriptionPolicy:
    """Market → 应订阅的 LiveSourceKey 列表。

    `sport_resolver` 由策略层注入（处理 market.category / sports_market_type /
    slug 关键词等运动识别逻辑）。`active_predicate` 决定是否值得为该 market 拉
    直播数据——典型实现按 game_start_time / end_date 判断 is_live / is_near_start /
    start_unknown（参 旧 sports_polling_demand._is_market_active）。

    `active_predicate` 默认始终为 True——纯按 sport 决定订阅。生产路径应注入
    实际活跃判断，避免 ended / 已结算市场仍触发轮询。
    """

    def __init__(
        self,
        *,
        sport_resolver: SportResolver,
        active_predicate: ActivePredicate = _always_active,
    ) -> None:
        self._resolve_sport = sport_resolver
        self._is_active = active_predicate

    def required_sources(self, market: Market) -> tuple[LiveSourceKey, ...]:
        if not self._is_active(market):
            return ()
        sport = self._resolve_sport(market)
        if not sport:
            return ()
        if sport in INPLAY_COVERED_SPORTS:
            return (
                LiveSourceKey(
                    provider=LiveSourceProvider.GOALSERVE_INPLAY,
                    sport=sport,
                ),
            )
        return (
            LiveSourceKey(
                provider=LiveSourceProvider.GOALSERVE_LIVESCORE,
                sport=sport,
            ),
        )
