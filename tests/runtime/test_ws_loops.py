from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Mapping

from polymarket_trader.runtime import ws_loops
from polymarket_trader.workers.user_ws_worker import UserWsWorker


class _FakeSupervisor:
    def heartbeat_worker(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _FakeScheduler:
    def __init__(self) -> None:
        self.triggers: list[str] = []

    def trigger_now(self, name: str) -> None:
        self.triggers.append(name)


class _FakePolymarketWsClient:
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
