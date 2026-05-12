from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from freezegun import freeze_time

from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.runtime.ws_loops import (
    market_ws_subscription_token_ids,
    user_ws_subscription_condition_ids,
)
from polymarket_trader.workers.market_ws import MarketWsWorker
from polymarket_trader.workers.user_ws import UserWsWorker

# 固定 snapshot 时间戳；snapshot.received_at 仅作 metadata。
_FIXED_NOW = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)


def test_market_ws_status_can_return_lightweight_summary() -> None:
    worker = MarketWsWorker()
    worker.track_market(_market())
    worker.build_subscription_request(("token-1-yes", "token-1-no"))

    status = worker.status_snapshot(include_subscriptions=False)

    assert status.tracked_market_count == 2
    assert status.subscription_count == 2
    assert status.tracked_token_ids == ()
    assert status.subscriptions == ()


def test_market_ws_status_connected_reflects_lifecycle_not_absence_of_error() -> None:
    """N12：worker 在 starting 阶段从未握手时，connected 必须为 False。

    旧逻辑用 ``last_error is None`` 推断 connected，导致 starting 阶段没有错误
    时被误判为已连接。修复后 connected 严格跟随 ``set_connection_state``：
    on_connect 才会置 True，任何 disconnect/reconnect 立刻置 False。
    """

    worker = MarketWsWorker()
    worker.track_market(_market())

    # starting 阶段：尚未握手，即便没有 last_error 也不能视为已连接。
    status = worker.status_snapshot(include_subscriptions=False)
    assert status.last_error is None
    assert status.connected is False

    worker.set_connection_state(True)
    assert worker.status_snapshot(include_subscriptions=False).connected is True
    assert worker.status_snapshot(include_subscriptions=True).connected is True

    worker.set_connection_state(False)
    assert worker.status_snapshot(include_subscriptions=False).connected is False


def test_market_ws_status_subscription_count_excludes_unsubscribed_tracked_markets() -> None:
    """F6：track_market 但未发起 WS 订阅时，subscription_count 不应包含这些 token。

    旧实现把 ``len(_tracked_markets)`` 当 subscribed_count 暴露给 metrics，
    "已 track 但未真正订阅" 的 token 会污染下游 ws_states.subscribed_count。
    """

    worker = MarketWsWorker()
    worker.track_market(_market(index=1))
    worker.track_market(_market(index=2))

    status_before = worker.status_snapshot(include_subscriptions=False)
    assert status_before.tracked_market_count == 4
    assert status_before.subscription_count == 0

    worker.build_subscription_request(("token-1-yes", "token-1-no"))
    status_after = worker.status_snapshot(include_subscriptions=False)
    assert status_after.tracked_market_count == 4
    assert status_after.subscription_count == 2


def test_user_ws_status_can_return_lightweight_summary() -> None:
    worker = UserWsWorker(strategy_id="sports_tail", )
    worker.build_subscription_request(
        ("condition-1", "condition-2"),
        auth={"apiKey": "key", "secret": "secret", "passphrase": "passphrase"},
    )

    status = worker.status_snapshot(include_subscriptions=False)

    assert status.subscription_count == 2
    assert status.subscribed_condition_ids == ()
    assert status.subscriptions == ()


@freeze_time("2026-05-12T00:00:00Z")
def test_market_ws_subscription_helper_keeps_only_live_or_held_markets() -> None:
    now = datetime.now(timezone.utc)
    registry = MarketRegistry()
    registry.upsert(_market(index=1))
    registry.upsert(_market(index=2))
    registry.upsert(_market(index=3))
    registry.upsert(_market(index=4, end_date=now + timedelta(hours=2)))
    registry.upsert(_market(index=5, end_date=now + timedelta(hours=2)))
    registry.upsert(_market(index=6))
    metadata_store = EntryMetadataStore()
    metadata_store.upsert(
        condition_id="condition-1",
        market_slug="nba-game-1-moneyline",
        metadata={"live_game": {"status": "live"}},
        source="test",
        live_state_signal_allowed=True,
        live_state_phase="live",
        live_state_payload={"status": "live"},
    )
    metadata_store.upsert(
        condition_id="condition-2",
        market_slug="nba-game-2-moneyline",
        metadata={"live_game": {"status": "scheduled"}},
        source="test",
        live_state_signal_allowed=False,
        live_state_signal_reason="sports_live_state_scheduled",
        live_state_phase="scheduled",
        live_state_payload={"status": "scheduled"},
    )
    metadata_store.upsert(
        condition_id="condition-4",
        market_slug="nba-game-4-moneyline",
        metadata={"live_game": {"status": "live"}},
        source="test",
        live_state_signal_allowed=None,
        live_state_phase="live",
        live_state_payload={"status": "live"},
    )
    metadata_store.upsert(
        condition_id="condition-5",
        market_slug="nba-game-5-moneyline",
        metadata={"live_game": {"status": "ended"}},
        source="test",
        live_state_signal_allowed=True,
        live_state_signal_reason="ended_not_closed",
        live_state_phase="ended",
        live_state_payload={"status": "ended"},
    )
    metadata_store.upsert(
        condition_id="condition-6",
        market_slug="nba-game-6-moneyline",
        metadata={"live_game": {"status": "ended"}},
        source="test",
        live_state_signal_allowed=False,
        live_state_signal_reason="series_market_pending_model",
        live_state_phase="ended",
        live_state_payload={"status": "ended"},
    )
    account_store = AccountStateStore()
    account_store.replace_positions(
        (
            Position(
                strategy_id="sports_tail",
                condition_id="condition-3",
                token_id="token-3-yes",
                shares=Decimal("5"),
                cost_usdc=Decimal("4"),
            ),
        )
    )
    runtime = SimpleNamespace(
        registry=registry,
        entry_metadata_store=metadata_store,
        account_state_store=account_store,
    )

    # 顺序已变成"按 end_date 升序"——near-end 优先，因为 _MARKET_WS_MAX_SUBSCRIPTIONS=200
    # 截断时要保留最快结束的。集合内容仍只覆盖有 live state 信号或持仓的 market。
    assert sorted(market_ws_subscription_token_ids(runtime)) == [
        "token-1-no",
        "token-1-yes",
        "token-3-no",
        "token-3-yes",
        "token-5-no",
        "token-5-yes",
    ]
    assert user_ws_subscription_condition_ids(runtime) == (
        "condition-1",
        "condition-2",
        "condition-3",
        "condition-4",
        "condition-5",
        "condition-6",
    )


def test_market_ws_worker_prefetches_rest_snapshot_for_empty_tracked_token() -> None:
    async def run() -> tuple[int, OrderbookSnapshot | None]:
        async def load_snapshot(token_id: str) -> OrderbookSnapshot:
            return OrderbookSnapshot(
                token_id=token_id,
                condition_id="condition-1",
                market_slug="nba-game-1-moneyline",
                best_bid=Decimal("0.52"),
                best_ask=Decimal("0.53"),
                best_bid_size=Decimal("100"),
                best_ask_size=Decimal("200"),
                bids=(PriceLevel(price=Decimal("0.52"), size=Decimal("100")),),
                asks=(PriceLevel(price=Decimal("0.53"), size=Decimal("200")),),
                received_at=_FIXED_NOW,
            )

        worker = MarketWsWorker(rest_snapshot_loader=load_snapshot)
        worker.track_market(_market())
        refreshed = await worker.refresh_rest_snapshots(("token-1-yes",))
        return refreshed, worker.snapshot("token-1-yes")

    refreshed, snapshot = asyncio.run(run())

    assert refreshed == 1
    assert snapshot is not None
    assert snapshot.best_bid == Decimal("0.52")
    assert snapshot.best_ask == Decimal("0.53")


def _market(index: int = 1, *, end_date: datetime | None = None) -> Market:
    return Market(
        condition_id=f"condition-{index}",
        market_slug=f"nba-game-{index}-moneyline",
        outcomes=(
            MarketOutcome(token_id=f"token-{index}-yes", outcome="Yes"),
            MarketOutcome(token_id=f"token-{index}-no", outcome="No"),
        ),
        end_date=end_date,
    )
