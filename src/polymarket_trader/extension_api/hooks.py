from __future__ import annotations

from typing import Protocol, runtime_checkable

from polymarket_trader.domain.market import Market
from polymarket_trader.extension_api.context import AccountSnapshotView, ExtensionContext
from polymarket_trader.extension_api.decisions import (
    EntrySizing,
    RecoveryDecision,
    ExtensionDecision,
    UniverseDecision,
)
from polymarket_trader.extension_api.discovery import DiscoveryQuery


@runtime_checkable
class ExtensionHooks(Protocol):
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
