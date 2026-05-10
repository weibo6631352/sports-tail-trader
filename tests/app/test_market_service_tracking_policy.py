from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trader.app.market_service import MarketService
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market
from polymarket_trader.extension_api import ExtensionContext
from polymarket_trader.extension_api.decisions import (
    EntrySizing,
    ExtensionDecision,
    RecoveryDecision,
    UniverseDecision,
)
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.market_ws import MarketWsWorker


class _IncludeAllHooks:
    def discovery_queries(self):
        return ()

    def select_market(self, market: Market) -> UniverseDecision:
        return UniverseDecision.include(reason="test")

    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        raise NotImplementedError

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="test")

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="test")

    def decide_recovery(self, context: ExtensionContext) -> RecoveryDecision:
        return RecoveryDecision(reason="test")

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return ()

    def should_keep_tracking(self, market: Market, account_snapshot: AccountSnapshot | None) -> bool:
        return False

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return existing_market


def _raw_market(*, end_date: datetime) -> dict[str, object]:
    return {
        "conditionId": "condition-1",
        "slug": "market-1",
        "question": "Sports market",
        "clobTokenIds": ["token-1-yes", "token-1-no"],
        "outcomes": ["YES", "NO"],
        "tickSize": "0.01",
        "orderMinSize": "5",
        "category": "Sports",
        "tags": ["Sports"],
        "endDate": end_date.isoformat(),
    }


def test_market_service_does_not_subscribe_new_expired_idle_market() -> None:
    registry = MarketRegistry()
    market_ws_worker = MarketWsWorker(registry=registry)
    service = MarketService(
        extension_hooks=_IncludeAllHooks(),
        registry=registry,
        market_tracker=market_ws_worker,
        account_snapshot_provider=lambda: AccountSnapshot(),
    )

    outcome = service.ingest_raw_market(
        _raw_market(end_date=datetime.now(timezone.utc) - timedelta(hours=1)),
        source="test",
        trace_id="trace-1",
    )

    assert outcome.accepted is False
    assert outcome.subscription_request is None
    assert outcome.event.reason == "market_end_date_elapsed"
    assert registry.snapshot().markets == ()
    assert market_ws_worker.status_snapshot().tracked_token_ids == ()
