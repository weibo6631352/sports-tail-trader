from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.runtime import ws_loops
from polymarket_trader.runtime.entry_metadata import EntryMetadataRecord
from polymarket_trader.workers.market_ws import MarketWsWorker
from polymarket_trader.workers.user_ws import UserWsWorker


class _FakeSupervisor:
    def heartbeat_worker(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _FakeScheduler:
    def __init__(self) -> None:
        self.triggers: list[str] = []

    def trigger_now(self, name: str) -> None:
        self.triggers.append(name)


class _FakeRegistry:
    def __init__(self, markets: tuple[Market, ...]) -> None:
        self._markets = markets

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(markets=self._markets)


class _FakeEntryMetadataStore:
    def __init__(self, record: EntryMetadataRecord) -> None:
        self._record = record

    def metadata_for(self, **_kwargs: Any) -> Mapping[str, Any]:
        return dict(self._record.metadata)

    def find(self, **_kwargs: Any) -> EntryMetadataRecord:
        return self._record


class _FakePolymarketWsClient:
    async def stream_market_messages(
        self,
        _token_ids: tuple[str, ...],
        *,
        reconnect: bool,
        on_connect: Any,
        on_disconnect: Any,
        on_reconnect: Any,
    ):
        assert reconnect is True
        assert on_disconnect
        assert on_reconnect
        await on_connect(0)
        if False:
            yield SimpleNamespace(payload={}, raw={})

    async def stream_user_messages(
        self,
        _condition_ids: tuple[str, ...],
        *,
        auth: Mapping[str, str],
        reconnect: bool,
        on_connect: Any,
        on_disconnect: Any,
        on_reconnect: Any,
    ):
        assert auth
        assert reconnect is True
        assert on_disconnect
        assert on_reconnect
        await on_connect(0)
        if False:
            yield SimpleNamespace(payload={}, raw={})


def test_stream_user_ws_messages_triggers_reconcile_after_connect(monkeypatch) -> None:
    monkeypatch.setattr(ws_loops, "sync_runtime_metrics", lambda _runtime: None)
    scheduler = _FakeScheduler()
    runtime = SimpleNamespace(
        polymarket_ws_client=_FakePolymarketWsClient(),
        scheduler=scheduler,
        supervisor=_FakeSupervisor(),
        user_ws_worker=UserWsWorker(strategy_id="sports_tail", ),
    )

    async def run() -> None:
        await ws_loops.stream_user_ws_messages(
            runtime,
            ("0xabc",),
            auth={"apiKey": "key", "secret": "secret", "passphrase": "pass"},
            queue=asyncio.Queue(),
        )

    asyncio.run(run())

    assert scheduler.triggers == ["periodic_reconcile"]


def test_stream_market_ws_messages_clears_stale_error_after_connect(monkeypatch) -> None:
    monkeypatch.setattr(ws_loops, "sync_runtime_metrics", lambda _runtime: None)
    worker = MarketWsWorker()
    worker.record_error("previous_prefetch_failed")
    runtime = SimpleNamespace(
        polymarket_ws_client=_FakePolymarketWsClient(),
        supervisor=_FakeSupervisor(),
        market_ws_worker=worker,
    )

    async def run() -> None:
        await ws_loops.stream_market_ws_messages(
            runtime,
            ("token-1",),
            queue=asyncio.Queue(),
        )

    asyncio.run(run())

    assert worker.status_snapshot(include_subscriptions=False).last_error is None


def test_market_ws_subscribes_strategy_allowed_tail_signal_even_when_gamma_end_date_is_far() -> None:
    market = Market(
        condition_id="tennis-first-set-condition",
        market_slug="atp-erhard-nedic-2026-04-29-first-set-winner-Erhard-vs-Nedic",
        event_slug="atp-erhard-nedic-2026-04-29",
        end_date=datetime(2026, 5, 6, 6, 0, tzinfo=timezone.utc),
        outcomes=(
            MarketOutcome(token_id="erhard-token", outcome="Erhard"),
            MarketOutcome(token_id="nedic-token", outcome="Nedic"),
        ),
    )
    runtime = SimpleNamespace(
        registry=_FakeRegistry((market,)),
        entry_metadata_store=_FakeEntryMetadataStore(
            EntryMetadataRecord(
                condition_id=market.condition_id,
                metadata={"live_game": {"status": "live", "period": "S2"}},
                live_state_signal_allowed=True,
                live_state_signal_reason="live_outcome_lock_candidate",
                live_state_phase="live",
                live_state_payload={"status": "live", "period": "S2"},
            )
        ),
        account_state_store=None,
    )

    token_ids = ws_loops.market_ws_subscription_token_ids(runtime)

    assert token_ids == ("erhard-token", "nedic-token")


def test_market_ws_subscribes_live_market_with_signal_allowed() -> None:
    market = Market(
        condition_id="tennis-live-condition",
        market_slug="atp-ghibaud-pieri-2026-04-29-first-set-winner-Ghibaudo-vs-Pieri",
        event_slug="atp-ghibaud-pieri-2026-04-29",
        end_date=datetime(2026, 5, 6, 6, 0, tzinfo=timezone.utc),
        outcomes=(
            MarketOutcome(token_id="ghibaudo-token", outcome="Ghibaudo"),
            MarketOutcome(token_id="pieri-token", outcome="Pieri"),
        ),
    )
    runtime = SimpleNamespace(
        registry=_FakeRegistry((market,)),
        entry_metadata_store=_FakeEntryMetadataStore(
            EntryMetadataRecord(
                condition_id=market.condition_id,
                metadata={"live_game": {"status": "live", "period": "S1"}},
                live_state_signal_allowed=True,
                live_state_signal_reason="live",
                live_state_phase="live",
                live_state_payload={"status": "live", "period": "S1"},
            )
        ),
        account_state_store=None,
    )

    token_ids = ws_loops.market_ws_subscription_token_ids(runtime)

    assert token_ids == ("ghibaudo-token", "pieri-token")


def test_market_ws_subscribes_outright_with_season_odds_despite_signal_allowed_false() -> None:
    """OUTRIGHT 市场有 season_odds_snapshot 时，signal_allowed=False 不应封锁订阅。

    market_end_too_far 场景之外的 signal_allowed=False 对 SINGLE_GAME 是硬封锁，但
    OUTRIGHT/SERIES 市场的元数据来自专用 worker，与 live_state 无关。市场若曾被误判为
    SINGLE_GAME、live_state_worker 写入 False 后又被重新分类，旧值不能永久封锁。
    """
    market = Market(
        condition_id="nba-champion-2026-condition",
        market_slug="nba-2026-champion-boston-celtics",
        event_slug="nba-2026-champion",
        end_date=datetime(2026, 5, 6, 6, 0, tzinfo=timezone.utc),  # 过去 → 仍允许订阅
        trading_status=TradingStatus.ELIGIBLE,
        outcomes=(
            MarketOutcome(token_id="celtics-token", outcome="Boston Celtics"),
            MarketOutcome(token_id="lakers-token", outcome="LA Lakers"),
        ),
    )
    runtime = SimpleNamespace(
        registry=_FakeRegistry((market,)),
        entry_metadata_store=_FakeEntryMetadataStore(
            EntryMetadataRecord(
                condition_id=market.condition_id,
                metadata={"season_odds_snapshot": {"fair_probabilities": {"Boston Celtics": "0.32"}}},
                live_state_signal_allowed=False,
                live_state_signal_reason="no_live_game_found",
                live_state_phase=None,
                live_state_payload={},
            )
        ),
        account_state_store=None,
    )

    token_ids = ws_loops.market_ws_subscription_token_ids(runtime)

    assert token_ids == ("celtics-token", "lakers-token")


def test_market_ws_blocks_single_game_with_signal_allowed_false() -> None:
    """SINGLE_GAME 的 signal_allowed=False 应保持封锁不变（scheduled/unknown 状态）。"""
    market = Market(
        condition_id="nba-game-condition",
        market_slug="nba-game-boston-vs-miami-2026-05-01",
        event_slug="nba-game-boston-vs-miami-2026-05-01",
        end_date=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
        trading_status=TradingStatus.ELIGIBLE,
        outcomes=(
            MarketOutcome(token_id="boston-token", outcome="Boston Celtics"),
            MarketOutcome(token_id="miami-token", outcome="Miami Heat"),
        ),
    )
    runtime = SimpleNamespace(
        registry=_FakeRegistry((market,)),
        entry_metadata_store=_FakeEntryMetadataStore(
            EntryMetadataRecord(
                condition_id=market.condition_id,
                metadata={"live_game": {"status": "scheduled"}},
                live_state_signal_allowed=False,
                live_state_signal_reason="game_not_started",
                live_state_phase="scheduled",
                live_state_payload={},
            )
        ),
        account_state_store=None,
    )

    token_ids = ws_loops.market_ws_subscription_token_ids(runtime)

    assert token_ids == ()


def test_market_ws_priority_token_ids_returns_exposed_tokens() -> None:
    """P3.2：持仓/挂单的 token_id 应出现在 priority 集合中。"""

    class _FakeAccountSnapshot:
        def __init__(self):
            self.positions = [
                type("Pos", (), {"condition_id": "cond-1", "token_id": "tok-1", "settled_zero_value": False})()
            ]
            self.open_orders = [
                type("Order", (), {"condition_id": "cond-2", "token_id": "tok-2", "settled_zero_value": False})()
            ]

    runtime = type("RT", (), {
        "account_state_store": type("S", (), {"snapshot": staticmethod(lambda: _FakeAccountSnapshot())})()
    })()

    priority = ws_loops.market_ws_priority_token_ids(runtime)

    assert "tok-1" in priority
    assert "tok-2" in priority


def test_market_ws_priority_token_ids_empty_without_account_store() -> None:
    """P3.2：无账户存储时 priority 集合为空（安全降级）。"""

    runtime = type("RT", (), {"account_state_store": None})()

    priority = ws_loops.market_ws_priority_token_ids(runtime)

    assert priority == frozenset()


def test_market_ws_priority_token_ids_excludes_settled_zero() -> None:
    """P3.2：settled_zero_value=True 的仓位不列入 priority。"""

    class _FakeAccountSnapshot:
        def __init__(self):
            self.positions = [
                type("Pos", (), {"condition_id": "cond-a", "token_id": "tok-a", "settled_zero_value": True})(),
                type("Pos", (), {"condition_id": "cond-b", "token_id": "tok-b", "settled_zero_value": False})(),
            ]
            self.open_orders = []

    runtime = type("RT", (), {
        "account_state_store": type("S", (), {"snapshot": staticmethod(lambda: _FakeAccountSnapshot())})()
    })()

    priority = ws_loops.market_ws_priority_token_ids(runtime)

    assert "tok-a" not in priority
    assert "tok-b" in priority
