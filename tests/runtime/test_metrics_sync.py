"""metrics_sync 写入 ws_states 时必须区分 tracked / subscribed / connected（N12, F6）。

- ``tracked_count`` 反映 track_market 已注册的 token 总数。
- ``subscribed_count`` 只算真正通过 WS 完成订阅（state.subscribed_at 非空）的 token。
- ``connected`` 直接读 worker lifecycle 标志，不再用 ``last_error is None`` 猜。
"""

from __future__ import annotations

from types import SimpleNamespace

from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.observability.metrics import MetricsRegistry
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.metrics_sync import sync_runtime_metrics
from polymarket_trader.workers.market_ws import MarketWsWorker
from polymarket_trader.workers.user_ws import UserWsWorker


def _make_market() -> Market:
    return Market(
        condition_id="condition-1",
        market_slug="nba-game-1-moneyline",
        outcomes=(
            MarketOutcome(token_id="token-1-yes", outcome="Yes"),
            MarketOutcome(token_id="token-1-no", outcome="No"),
        ),
    )


def _runtime(
    *,
    market_ws_worker: MarketWsWorker,
    user_ws_worker: UserWsWorker,
    metrics: MetricsRegistry,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_bus=EventBus(),
        metrics=metrics,
        market_ws_worker=market_ws_worker,
        user_ws_worker=user_ws_worker,
        reconcile_worker=SimpleNamespace(
            status_snapshot=lambda: SimpleNamespace(last_completed_at=None)
        ),
        market_discovery_scan=SimpleNamespace(
            round_id=1,
            pages_scanned_in_round=0,
            markets_seen_in_round=0,
            last_page_size=0,
            after_cursor=None,
            query_cursors={},
            completed_query_names=set(),
            last_tick_requests=0,
            last_tick_markets=0,
            consecutive_failures=0,
            last_round_completed_at=None,
        ),
        persistence_worker=SimpleNamespace(
            snapshot=lambda: SimpleNamespace(
                outbox_depth=0,
                outbox_retained_depth=0,
                outbox_dead_letter_depth=0,
                retried_events=0,
            )
        ),
    )


def test_metrics_sync_marks_market_ws_disconnected_when_handshake_pending() -> None:
    """N12：worker 在 starting 阶段未握手时，metrics.connected 必须为 False。"""

    market_ws_worker = MarketWsWorker()
    market_ws_worker.track_market(_make_market())  # 注册 token 但没有 set_connection_state(True)
    metrics = MetricsRegistry()
    runtime = _runtime(
        market_ws_worker=market_ws_worker,
        user_ws_worker=UserWsWorker(strategy_id="sports_tail"),
        metrics=metrics,
    )

    sync_runtime_metrics(runtime)

    state = metrics.ws_state("market_ws")
    assert state is not None
    assert state.connected is False, "starting 阶段未握手不能视为已连接（N12）"


def test_metrics_sync_distinguishes_tracked_vs_subscribed_counts() -> None:
    """F6：tracked_count = 注册关注 token；subscribed_count = 真正 WS 订阅 token。"""

    market_ws_worker = MarketWsWorker()
    market_ws_worker.track_market(_make_market())
    # 只订阅一边的 token，另一边只 track 不订阅。
    market_ws_worker.build_subscription_request(("token-1-yes",))
    market_ws_worker.set_connection_state(True)
    metrics = MetricsRegistry()
    runtime = _runtime(
        market_ws_worker=market_ws_worker,
        user_ws_worker=UserWsWorker(strategy_id="sports_tail"),
        metrics=metrics,
    )

    sync_runtime_metrics(runtime)

    state = metrics.ws_state("market_ws")
    assert state is not None
    assert state.connected is True
    assert state.tracked_count == 2, "track_market 注册了 2 个 token"
    assert state.subscribed_count == 1, "只有 1 个 token 真正完成 WS 订阅请求"


def test_metrics_sync_reflects_market_ws_connected_after_lifecycle_hook() -> None:
    """N12：set_connection_state(True) 后 metrics.connected 立刻反映为 True。"""

    market_ws_worker = MarketWsWorker()
    market_ws_worker.track_market(_make_market())
    market_ws_worker.set_connection_state(True)
    metrics = MetricsRegistry()
    runtime = _runtime(
        market_ws_worker=market_ws_worker,
        user_ws_worker=UserWsWorker(strategy_id="sports_tail"),
        metrics=metrics,
    )

    sync_runtime_metrics(runtime)
    state = metrics.ws_state("market_ws")
    assert state is not None
    assert state.connected is True

    market_ws_worker.set_connection_state(False)
    sync_runtime_metrics(runtime)
    state_after = metrics.ws_state("market_ws")
    assert state_after is not None
    assert state_after.connected is False


def test_metrics_sync_keeps_user_ws_connected_field_intact() -> None:
    """user_ws.connected 仍然取自 account_state，metrics_sync 不能反向把它误改。"""

    metrics = MetricsRegistry()
    account_state = AccountStateStore()
    user_ws_worker = UserWsWorker(
        strategy_id="sports_tail",
        account_state_store=account_state,
    )
    account_state.mark_user_ws_connected(True)
    runtime = _runtime(
        market_ws_worker=MarketWsWorker(),
        user_ws_worker=user_ws_worker,
        metrics=metrics,
    )

    sync_runtime_metrics(runtime)
    state = metrics.ws_state("user_ws")
    assert state is not None
    assert state.connected is True
