from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Mapping
from uuid import uuid4

from polymarket_trader.runtime.metrics_sync import sync_runtime_metrics
from polymarket_trader.runtime.status import WorkerLifecycleState

_SUBSCRIPTION_REFRESH_SECONDS = 5.0


def market_ws_subscription_token_ids(runtime: Any) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                token_id
                for market in runtime.registry.snapshot().markets
                for token_id in market.token_ids
                if token_id
            }
        )
    )


def user_ws_subscription_condition_ids(runtime: Any) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                market.condition_id
                for market in runtime.registry.snapshot().markets
                if market.condition_id
            }
        )
    )


def user_ws_auth_payload(runtime: Any) -> dict[str, str] | None:
    if runtime.trading_client is None:
        return None
    credentials = runtime.trading_client.get_api_credentials()
    return {
        "apiKey": credentials.api_key,
        "secret": credentials.api_secret,
        "passphrase": credentials.api_passphrase,
    }


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


def _drain_queue(queue: asyncio.Queue[Mapping[str, Any]]) -> None:
    while not queue.empty():
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()


def _trigger_reconcile_after_user_ws_connect(runtime: Any) -> None:
    """User WS 恢复后立即唤醒账户 reconcile，避免等待下一次周期调度。"""

    scheduler = getattr(runtime, "scheduler", None)
    if scheduler is None:
        return
    with suppress(KeyError):
        scheduler.trigger_now("periodic_reconcile")


async def stream_market_ws_messages(
    runtime: Any,
    token_ids: tuple[str, ...],
    queue: asyncio.Queue[Mapping[str, Any]],
) -> None:
    async def on_connect(attempt: int) -> None:
        runtime.supervisor.heartbeat_worker(
            "market_ws",
            state=WorkerLifecycleState.RUNNING,
            healthy=True,
            detail=f"connected subscribed={len(token_ids)} attempt={attempt}",
        )
        sync_runtime_metrics(runtime)

    async def on_disconnect(attempt: int) -> None:
        runtime.supervisor.heartbeat_worker(
            "market_ws",
            state=WorkerLifecycleState.PAUSED,
            healthy=True,
            detail=f"disconnected attempt={attempt}",
        )
        sync_runtime_metrics(runtime)

    async def on_reconnect(attempt: int, exc: Exception) -> None:
        runtime.market_ws_worker.record_error(str(exc))
        runtime.supervisor.heartbeat_worker(
            "market_ws",
            state=WorkerLifecycleState.DEGRADED,
            healthy=False,
            detail=f"reconnecting attempt={attempt}",
            last_error=str(exc),
        )
        sync_runtime_metrics(runtime)

    async for message in runtime.polymarket_ws_client.stream_market_messages(
        token_ids,
        reconnect=True,
        on_connect=on_connect,
        on_disconnect=on_disconnect,
        on_reconnect=on_reconnect,
    ):
        payload = message.payload if isinstance(message.payload, Mapping) else message.raw
        await queue.put(dict(payload))


def market_ws_message_type(message: Mapping[str, Any]) -> str:
    value = (
        message.get("event_type")
        or message.get("message_type")
        or message.get("channel_event")
        or message.get("event")
        or message.get("type")
        or message.get("action")
    )
    return "" if value is None else str(value).strip().lower()


async def handle_market_ws_message(
    runtime: Any,
    message: Mapping[str, Any],
) -> None:
    if market_ws_message_type(message) == "new_market":
        await runtime.market_discovery_worker.ingest_ws_new_market(
            message,
            trace_id=f"market-ws-discovery-{uuid4().hex}",
        )
    await runtime.market_ws_worker.handle_message(message, source="market_ws")


async def stream_user_ws_messages(
    runtime: Any,
    condition_ids: tuple[str, ...],
    auth: Mapping[str, str],
    queue: asyncio.Queue[Mapping[str, Any]],
) -> None:
    async def on_connect(attempt: int) -> None:
        await runtime.user_ws_worker.set_connection_state(
            True,
            trace_id=f"user-ws-connected-{uuid4().hex}",
            reason="user_ws_connected",
        )
        _trigger_reconcile_after_user_ws_connect(runtime)
        runtime.supervisor.heartbeat_worker(
            "user_ws",
            state=WorkerLifecycleState.RUNNING,
            healthy=True,
            detail=f"connected subscribed={len(condition_ids)} attempt={attempt}",
        )
        sync_runtime_metrics(runtime)

    async def on_disconnect(attempt: int) -> None:
        await runtime.user_ws_worker.set_connection_state(
            False,
            trace_id=f"user-ws-disconnected-{uuid4().hex}",
            reason="user_ws_disconnected",
        )
        runtime.supervisor.heartbeat_worker(
            "user_ws",
            state=WorkerLifecycleState.PAUSED,
            healthy=True,
            detail=f"disconnected attempt={attempt}",
        )
        sync_runtime_metrics(runtime)

    async def on_reconnect(attempt: int, exc: Exception) -> None:
        runtime.user_ws_worker.record_error(str(exc))
        await runtime.user_ws_worker.set_connection_state(
            False,
            trace_id=f"user-ws-reconnecting-{uuid4().hex}",
            reason="user_ws_reconnecting",
        )
        runtime.supervisor.heartbeat_worker(
            "user_ws",
            state=WorkerLifecycleState.DEGRADED,
            healthy=False,
            detail=f"reconnecting attempt={attempt}",
            last_error=str(exc),
        )
        sync_runtime_metrics(runtime)

    async for message in runtime.polymarket_ws_client.stream_user_messages(
        condition_ids,
        auth=auth,
        reconnect=True,
        on_connect=on_connect,
        on_disconnect=on_disconnect,
        on_reconnect=on_reconnect,
    ):
        payload = message.payload if isinstance(message.payload, Mapping) else message.raw
        await queue.put(dict(payload))


async def run_market_ws(runtime: Any) -> None:
    queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=512)
    stream_task: asyncio.Task[None] | None = None
    subscribed_token_ids: tuple[str, ...] = ()
    next_subscription_refresh_at = 0.0
    try:
        while True:
            loop = asyncio.get_running_loop()
            if stream_task is not None and stream_task.done():
                exc = None if stream_task.cancelled() else stream_task.exception()
                if exc is not None:
                    runtime.market_ws_worker.record_error(str(exc))
                    runtime.supervisor.mark_worker_error(
                        "market_ws",
                        detail="stream_failed",
                        last_error=str(exc),
                    )
                stream_task = None
                subscribed_token_ids = ()
                next_subscription_refresh_at = 0.0

            if loop.time() >= next_subscription_refresh_at:
                next_subscription_refresh_at = loop.time() + _SUBSCRIPTION_REFRESH_SECONDS
                desired_token_ids = market_ws_subscription_token_ids(runtime)
                if desired_token_ids != subscribed_token_ids:
                    await _cancel_task(stream_task)
                    stream_task = None
                    subscribed_token_ids = ()
                    _drain_queue(queue)
                    if desired_token_ids:
                        runtime.market_ws_worker.build_subscription_request(desired_token_ids)
                        stream_task = asyncio.create_task(
                            stream_market_ws_messages(runtime, desired_token_ids, queue),
                            name="trader:market-ws-stream",
                        )
                        subscribed_token_ids = desired_token_ids
                        runtime.supervisor.heartbeat_worker(
                            "market_ws",
                            state=WorkerLifecycleState.RUNNING,
                            healthy=True,
                            detail=f"subscribing count={len(subscribed_token_ids)}",
                        )
                    else:
                        runtime.supervisor.heartbeat_worker(
                            "market_ws",
                            state=WorkerLifecycleState.PAUSED,
                            healthy=True,
                            detail="no_markets",
                        )
                        sync_runtime_metrics(runtime)

            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue

            await handle_market_ws_message(runtime, message)
            runtime.supervisor.heartbeat_worker(
                "market_ws",
                state=WorkerLifecycleState.RUNNING,
                healthy=True,
                detail=f"subscribed={len(subscribed_token_ids)}",
            )
            sync_runtime_metrics(runtime)
    except asyncio.CancelledError:
        await _cancel_task(stream_task)
        runtime.supervisor.heartbeat_worker(
            "market_ws",
            state=WorkerLifecycleState.STOPPED,
            healthy=True,
            detail="cancelled",
        )
        raise


async def run_user_ws(runtime: Any) -> None:
    queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=512)
    stream_task: asyncio.Task[None] | None = None
    subscribed_condition_ids: tuple[str, ...] = ()
    subscribed_auth: dict[str, str] | None = None
    next_subscription_refresh_at = 0.0
    try:
        while True:
            loop = asyncio.get_running_loop()
            if stream_task is not None and stream_task.done():
                exc = None if stream_task.cancelled() else stream_task.exception()
                if exc is not None:
                    runtime.user_ws_worker.record_error(str(exc))
                    runtime.supervisor.mark_worker_error(
                        "user_ws",
                        detail="stream_failed",
                        last_error=str(exc),
                    )
                stream_task = None
                subscribed_condition_ids = ()
                subscribed_auth = None
                next_subscription_refresh_at = 0.0

            if loop.time() >= next_subscription_refresh_at:
                next_subscription_refresh_at = loop.time() + _SUBSCRIPTION_REFRESH_SECONDS
                desired_condition_ids = user_ws_subscription_condition_ids(runtime)
                try:
                    desired_auth = user_ws_auth_payload(runtime) if desired_condition_ids else None
                except Exception as exc:
                    runtime.user_ws_worker.record_error(str(exc))
                    await runtime.user_ws_worker.set_connection_state(
                        False,
                        trace_id=f"user-ws-auth-failed-{uuid4().hex}",
                        reason="user_ws_auth_failed",
                    )
                    runtime.supervisor.mark_worker_error(
                        "user_ws",
                        detail="auth_failed",
                        last_error=str(exc),
                    )
                    sync_runtime_metrics(runtime)
                    await asyncio.sleep(5.0)
                    continue

                if desired_condition_ids != subscribed_condition_ids or desired_auth != subscribed_auth:
                    await _cancel_task(stream_task)
                    stream_task = None
                    subscribed_condition_ids = ()
                    subscribed_auth = None
                    _drain_queue(queue)
                    if desired_condition_ids and desired_auth is not None:
                        runtime.user_ws_worker.build_subscription_request(
                            desired_condition_ids,
                            auth=desired_auth,
                        )
                        stream_task = asyncio.create_task(
                            stream_user_ws_messages(runtime, desired_condition_ids, desired_auth, queue),
                            name="trader:user-ws-stream",
                        )
                        subscribed_condition_ids = desired_condition_ids
                        subscribed_auth = dict(desired_auth)
                        runtime.supervisor.heartbeat_worker(
                            "user_ws",
                            state=WorkerLifecycleState.RUNNING,
                            healthy=True,
                            detail=f"subscribing count={len(subscribed_condition_ids)}",
                        )
                    else:
                        await runtime.user_ws_worker.set_connection_state(
                            False,
                            trace_id=f"user-ws-paused-{uuid4().hex}",
                            reason="user_ws_not_started",
                        )
                        runtime.supervisor.heartbeat_worker(
                            "user_ws",
                            state=WorkerLifecycleState.PAUSED,
                            healthy=True,
                            detail="no_markets_or_auth",
                        )
                        sync_runtime_metrics(runtime)

            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue

            await runtime.user_ws_worker.process_message(message)
            runtime.supervisor.heartbeat_worker(
                "user_ws",
                state=WorkerLifecycleState.RUNNING,
                healthy=True,
                detail=f"subscribed={len(subscribed_condition_ids)}",
            )
            sync_runtime_metrics(runtime)
    except asyncio.CancelledError:
        await _cancel_task(stream_task)
        await runtime.user_ws_worker.set_connection_state(
            False,
            trace_id=f"user-ws-stopped-{uuid4().hex}",
            reason="user_ws_stopped",
        )
        runtime.supervisor.heartbeat_worker(
            "user_ws",
            state=WorkerLifecycleState.STOPPED,
            healthy=True,
            detail="cancelled",
        )
        raise
