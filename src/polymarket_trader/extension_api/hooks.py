from __future__ import annotations

from typing import Protocol, runtime_checkable

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import LiveEvent
from polymarket_trader.extension_api.context import AccountSnapshotView, ExtensionContext
from polymarket_trader.extension_api.decisions import (
    EntrySizing,
    RecoveryDecision,
    ExtensionDecision,
    UniverseDecision,
)
from polymarket_trader.extension_api.discovery import DiscoveryQuery
from polymarket_trader.extension_api.live_state import LiveStateMatch


@runtime_checkable
class ExtensionHooks(Protocol):
    """所有策略必须实现的核心决策契约。

    注意：体育直播 / 比分源消费是可选能力，独立到 ``LiveStateHooks``；这里不强制。
    """

    def discovery_queries(self) -> tuple[DiscoveryQuery, ...]: ...

    def select_market(self, market: Market) -> UniverseDecision: ...

    def size_entry(self, context: ExtensionContext) -> EntrySizing: ...

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision: ...

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision: ...

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision: ...

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]: ...

    def should_keep_tracking(
        self,
        market: Market,
        account_snapshot: AccountSnapshotView | None,
    ) -> bool: ...

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market: ...


@runtime_checkable
class LiveStateHooks(Protocol):
    """可选：策略消费 framework 的体育直播状态时实现。

    framework 通过 ``BusinessExtension.live_state_hooks`` 拿到这个对象；返回 None
    表示策略不参与直播驱动的市场发现 / 跟踪，framework 会跳过 ``SportsLiveStateWorker``
    的装配，不强制非体育策略实现这两个 hook。
    """

    def discovery_queries_for_live_events(
        self, events: tuple[LiveEvent, ...]
    ) -> tuple[DiscoveryQuery, ...]: ...

    def match_live_state(
        self, market: Market, events: tuple[LiveEvent, ...]
    ) -> LiveStateMatch | None: ...
