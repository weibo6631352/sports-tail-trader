from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api.decisions import UniverseDecision
from polymarket_trader.main import _restore_trackable_markets
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.market_ws import MarketWsWorker


class _RejectingHooks:
    def select_market(self, _market: Market) -> UniverseDecision:
        return UniverseDecision.exclude(reason="esports_market_not_auto_tradable")

    def should_keep_tracking(self, market, account_snapshot) -> bool:
        for token_id in market.token_ids:
            position = account_snapshot.get_position(market.condition_id, token_id)
            if position is not None and position.shares > 0:
                return True
        return False

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return candidate_market.with_trading_status(TradingStatus.PAUSED, reject_reason=reason)


def test_startup_reference_restore_drops_stale_unselected_market_without_exposure() -> None:
    registry = MarketRegistry()
    runtime = _runtime(registry=registry)

    restored = _restore_trackable_markets(runtime, (_market(),))

    assert restored == 0
    assert registry.snapshot().markets == ()


def test_startup_reference_restore_keeps_stale_unselected_market_with_exposure() -> None:
    registry = MarketRegistry()
    account_state_store = AccountStateStore()
    account_state_store.replace_positions(
        (
            Position(
                condition_id="condition-1",
                token_id="token-1-yes",
                shares=Decimal("1"),
                cost_usdc=Decimal("1"),
            ),
        )
    )
    runtime = _runtime(registry=registry, account_state_store=account_state_store)

    restored = _restore_trackable_markets(runtime, (_market(),))

    assert restored == 1
    tracked_market = registry.snapshot().markets[0]
    assert tracked_market.trading_status == TradingStatus.PAUSED
    assert tracked_market.reject_reason == "esports_market_not_auto_tradable"


def _runtime(
    *,
    registry: MarketRegistry,
    account_state_store: AccountStateStore | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        extension=SimpleNamespace(hooks=_RejectingHooks()),
        account_state_store=account_state_store or AccountStateStore(),
        market_ws_worker=MarketWsWorker(registry=registry),
    )


def _market() -> Market:
    return Market(
        condition_id="condition-1",
        market_slug="lol-stale-market",
        outcomes=(
            MarketOutcome(token_id="token-1-yes", outcome="YES"),
            MarketOutcome(token_id="token-1-no", outcome="NO"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
