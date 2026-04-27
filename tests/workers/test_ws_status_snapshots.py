from __future__ import annotations

from types import SimpleNamespace

from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.runtime.ws_loops import (
    market_ws_subscription_token_ids,
    user_ws_subscription_condition_ids,
)
from polymarket_trader.workers.market_ws_worker import MarketWsWorker
from polymarket_trader.workers.user_ws_worker import UserWsWorker


def test_market_ws_status_can_return_lightweight_summary() -> None:
    worker = MarketWsWorker()
    worker.track_market(_market())
    worker.build_subscription_request(("token-1-yes", "token-1-no"))

    status = worker.status_snapshot(include_subscriptions=False)

    assert status.tracked_market_count == 2
    assert status.subscription_count == 2
    assert status.tracked_token_ids == ()
    assert status.subscriptions == ()


def test_user_ws_status_can_return_lightweight_summary() -> None:
    worker = UserWsWorker()
    worker.build_subscription_request(
        ("condition-1", "condition-2"),
        auth={"apiKey": "key", "secret": "secret", "passphrase": "passphrase"},
    )

    status = worker.status_snapshot(include_subscriptions=False)

    assert status.subscription_count == 2
    assert status.subscribed_condition_ids == ()
    assert status.subscriptions == ()


def test_ws_subscription_helpers_keep_full_registry_scope() -> None:
    registry = MarketRegistry()
    registry.upsert(_market(index=1))
    registry.upsert(_market(index=2))
    runtime = SimpleNamespace(registry=registry)

    assert market_ws_subscription_token_ids(runtime) == (
        "token-1-no",
        "token-1-yes",
        "token-2-no",
        "token-2-yes",
    )
    assert user_ws_subscription_condition_ids(runtime) == ("condition-1", "condition-2")


def _market(index: int = 1) -> Market:
    return Market(
        condition_id=f"condition-{index}",
        market_slug=f"nba-game-{index}-moneyline",
        outcomes=(
            MarketOutcome(token_id=f"token-{index}-yes", outcome="Yes"),
            MarketOutcome(token_id=f"token-{index}-no", outcome="No"),
        ),
    )
