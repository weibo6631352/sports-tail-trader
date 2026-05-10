from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping

from polymarket_trader.domain.market import Market, MarketOutcome
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
        user_ws_worker=UserWsWorker(),
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


def test_market_ws_prewarms_live_market_even_before_tail_signal_window() -> None:
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
                live_state_signal_allowed=False,
                live_state_signal_reason="market_end_too_far",
                live_state_phase="live",
                live_state_payload={"status": "live", "period": "S1"},
            )
        ),
        account_state_store=None,
    )

    token_ids = ws_loops.market_ws_subscription_token_ids(runtime)

    assert token_ids == ("ghibaudo-token", "pieri-token")
