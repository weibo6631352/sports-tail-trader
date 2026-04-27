from __future__ import annotations

from decimal import Decimal

from polymarket_trader.app.market_service import MarketService
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api import ExtensionSpec, UniverseDecision
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.registry import MarketRegistry


class _Tracker:
    def __init__(self) -> None:
        self.markets = []
        self.untracked_token_ids = []

    def track_market(self, market) -> None:
        self.markets.append(market)

    def untrack_market(self, token_id: str) -> None:
        self.untracked_token_ids.append(token_id)

    def build_subscription_request(self, token_id: str | tuple[str, ...]) -> dict[str, object]:
        token_ids = [token_id] if isinstance(token_id, str) else list(token_id)
        return {
            "channel": "market",
            "token_ids": token_ids,
            "custom_feature_enabled": True,
        }


def _raw_market(*, condition_id: str = "condition", market_slug: str = "sample-market-a") -> dict[str, str]:
    return {
        "category": "Crypto",
        "eventTitle": "Will this market reach a threshold?",
        "question": "Will this market hit a threshold?",
        "slug": market_slug,
        "conditionId": condition_id,
        "clobTokenIds": [f"yes-{condition_id}", f"no-{condition_id}"],
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "1",
    }


class _AcceptingStrategy:
    @property
    def spec(self):
        return ExtensionSpec(name="accepting")

    def select_market(self, market):
        return UniverseDecision.include(reason="accepted")

    def size_entry(self, context):
        raise AssertionError("not used")

    def decide_entry(self, context):
        raise AssertionError("not used")

    def decide_exit(self, context):
        raise AssertionError("not used")

    def decide_recovery(self, context):
        raise AssertionError("not used")

    def should_keep_tracking(self, market, account_snapshot):
        return True

    def build_filtered_tracking_market(self, candidate_market, *, existing_market, reason):
        return candidate_market.with_trading_status(
            TradingStatus.PAUSED,
            reject_reason=reason or "market_out_of_universe",
        )


class _SwitchingStrategy:
    def __init__(self, *, selected: bool) -> None:
        self.selected = selected

    @property
    def spec(self):
        return ExtensionSpec(name="switching")

    def select_market(self, market):
        if self.selected:
            return UniverseDecision.include(reason="accepted")
        return UniverseDecision.exclude(reason="market_out_of_universe")

    def size_entry(self, context):
        raise AssertionError("not used")

    def decide_entry(self, context):
        raise AssertionError("not used")

    def decide_exit(self, context):
        raise AssertionError("not used")

    def decide_recovery(self, context):
        raise AssertionError("not used")

    def should_keep_tracking(self, market, account_snapshot):
        if account_snapshot is None:
            return True
        token_id = market.require_token_id("NO")
        position = account_snapshot.get_position(market.condition_id, token_id)
        if position is not None and position.shares > 0:
            return True
        return bool(account_snapshot.open_orders_for_market(market.condition_id, token_id))

    def build_filtered_tracking_market(self, candidate_market, *, existing_market, reason):
        return candidate_market.with_trading_status(
            TradingStatus.PAUSED,
            reject_reason=reason or "market_out_of_universe",
        )


def test_market_service_ingests_market_into_registry_and_tracker() -> None:
    registry = MarketRegistry()
    tracker = _Tracker()
    service = MarketService(
        extension_hooks=_AcceptingStrategy(),
        registry=registry,
        market_tracker=tracker,
    )

    outcome = service.ingest_raw_market(_raw_market(), source="gamma", trace_id="trace")

    assert outcome.accepted
    assert outcome.parse_result.accepted
    assert outcome.market is not None
    assert outcome.discovery_kind == DomainEventType.MARKET_DISCOVERED.value
    assert outcome.event.event_type == DomainEventType.MARKET_DISCOVERED
    assert registry.get_by_condition_id("condition") == outcome.market
    assert tracker.markets == [outcome.market]
    assert outcome.event.payload["parse_status"] == "accepted"
    assert outcome.event.payload["parse_reason"] is None
    assert outcome.event.payload["parse_detail"] is None
    assert outcome.subscription_request == tracker.build_subscription_request(
        ("yes-condition", "no-condition")
    )


def test_market_service_marks_existing_market_as_updated() -> None:
    registry = MarketRegistry()
    service = MarketService(
        extension_hooks=_AcceptingStrategy(),
        registry=registry,
    )

    first = service.ingest_raw_market(_raw_market(), source="gamma", trace_id="trace-1")
    second = service.ingest_raw_market(_raw_market(), source="gamma", trace_id="trace-2")

    assert first.accepted
    assert second.accepted
    assert second.discovery_kind == DomainEventType.MARKET_UPDATED.value
    assert second.event.event_type == DomainEventType.MARKET_UPDATED


def test_market_service_respects_extension_universe_filter() -> None:
    class _RejectingStrategy:
        @property
        def spec(self):
            return ExtensionSpec(name="rejecting")

        def select_market(self, market):
            return UniverseDecision.exclude(reason="market_out_of_universe")

        def size_entry(self, context):
            raise AssertionError("not used")

        def decide_entry(self, context):
            raise AssertionError("not used")

        def decide_exit(self, context):
            raise AssertionError("not used")

        def decide_recovery(self, context):
            raise AssertionError("not used")

        def should_keep_tracking(self, market, account_snapshot):
            return True

        def build_filtered_tracking_market(self, candidate_market, *, existing_market, reason):
            return candidate_market.with_trading_status(
                TradingStatus.PAUSED,
                reject_reason=reason or "market_out_of_universe",
            )

    registry = MarketRegistry()
    tracker = _Tracker()
    service = MarketService(
        extension_hooks=_RejectingStrategy(),
        registry=registry,
        market_tracker=tracker,
    )

    outcome = service.ingest_raw_market(_raw_market(), source="gamma", trace_id="trace")

    assert outcome.accepted is False
    assert outcome.market is None
    assert outcome.parse_result.accepted
    assert outcome.event.event_type == DomainEventType.MARKET_FILTERED_OUT
    assert outcome.event.reason == "market_out_of_universe"
    assert outcome.should_publish_event is False
    assert tracker.markets == []


def test_market_service_keeps_filtered_existing_market_paused_while_exposure_remains() -> None:
    registry = MarketRegistry()
    tracker = _Tracker()
    strategy = _SwitchingStrategy(selected=True)
    account_state_store = AccountStateStore()
    account_state_store.upsert_position(
        Position(
            condition_id="condition",
            token_id="no-condition",
            market_slug="sample-market-a",
            shares=Decimal("5"),
            cost_usdc=Decimal("3"),
        )
    )
    service = MarketService(
        extension_hooks=strategy,
        registry=registry,
        market_tracker=tracker,
        account_snapshot_provider=account_state_store.snapshot,
    )

    first = service.ingest_raw_market(_raw_market(), source="gamma", trace_id="trace-1")
    strategy.selected = False
    outcome = service.ingest_raw_market(_raw_market(), source="gamma", trace_id="trace-2")

    assert first.accepted
    assert outcome.accepted is False
    assert outcome.event.event_type == DomainEventType.MARKET_FILTERED_OUT
    assert outcome.tracking_retained is True
    assert outcome.should_publish_event is True
    retained = registry.get_by_condition_id("condition")
    assert retained is not None
    assert retained.trading_status == TradingStatus.PAUSED
    assert retained.reject_reason == "market_out_of_universe"
    assert outcome.event.payload["tracking_retained"] is True
    assert outcome.event.payload["tracked_market"]["trading_status"] == TradingStatus.PAUSED.value
    assert tracker.untracked_token_ids == []


def test_market_service_removes_filtered_existing_market_when_flat_and_orderless() -> None:
    registry = MarketRegistry()
    tracker = _Tracker()
    strategy = _SwitchingStrategy(selected=True)
    account_state_store = AccountStateStore()
    service = MarketService(
        extension_hooks=strategy,
        registry=registry,
        market_tracker=tracker,
        account_snapshot_provider=account_state_store.snapshot,
    )

    first = service.ingest_raw_market(_raw_market(), source="gamma", trace_id="trace-1")
    assert first.accepted

    strategy.selected = False
    outcome = service.ingest_raw_market(_raw_market(), source="gamma", trace_id="trace-2")

    assert outcome.accepted is False
    assert outcome.tracking_retained is False
    assert outcome.tracking_removed is True
    assert outcome.should_publish_event is True
    assert outcome.event.event_type == DomainEventType.MARKET_FILTERED_OUT
    assert registry.get_by_condition_id("condition") is None
    assert tracker.untracked_token_ids == [("yes-condition", "no-condition")]
    assert outcome.event.payload["tracking_retained"] is False
    assert outcome.event.payload["tracked_market"] is None
