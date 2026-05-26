from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timezone
from typing import TYPE_CHECKING, Any, Mapping
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.runtime.metrics_sync import sync_runtime_metrics
from polymarket_trader.runtime.status import WorkerLifecycleState

logger = logging.getLogger(__name__)

_SUBSCRIPTION_REFRESH_SECONDS = 5.0
# 订阅数上限（按 token 计）——安全护栏，防止 registry 全量压垮 Polymarket WS。
# 订阅 gate 直接从 entry_metadata 实时算（live phase + signal_allowed），不再
# 依赖 Market.ws_eligible flag；超出时按 end_date 升序截断（最快结束的优先）。
_MARKET_WS_MAX_SUBSCRIPTIONS = 8000
# stream task 已启动后容忍订阅集的小幅变化，避免持续 cancel/reconnect。
# 只有 added+removed > 此阈值才重建连接；新增 token 会在下次 reconnect 时补充。
_MARKET_WS_RESUBSCRIBE_THRESHOLD = 10


def should_subscribe_ws(
    market: Any,
    entry_metadata: Any,
    has_exposure: bool,
) -> bool:
    """单一 WS 订阅 gate——CLAUDE.md §0 推 vs 拉、§19 简化反应式架构的应用。

    决策逻辑：
    1. **有持仓 / 挂单** → 必须订阅。即便比赛已结束，exit overlay / settlement 需要
       盘口推送来决定最后挂单时机；持仓走 exposure override 绕过其他 gate。
    2. **市场被人工/自动 pause** → 不订阅。trading_status != ELIGIBLE 表示框架明确
       说"不要对这个市场下单"，订阅 WS 也没决策价值。
    3. **没有 entry_metadata** → 不订阅。说明 live_state_worker 从来没匹配上这个
       market（discovery 找到但没直播数据），盘口推送对决策没意义。
    4. **live_state_signal_allowed=False** → 不订阅。worker 明确表示该 market
       不应发交易信号（赔率缺失/未开始/等结算/等）。当前等价于 phase 检查的
       子集,保留为独立语义闸门——表达"被市场门限拒绝"的意图,对未来引入
       "LIVE 但赔率缺失也拒"等扩展更安全。
    5. **live_state_phase != "live"** → 不订阅。phase=ended/paused/scheduled 都
       不该订阅。phase 缺失（""）只在 OUTRIGHT 等无直播概念市场出现——目前
       不予订阅（OUTRIGHT 决策不依赖盘口高频推送，定期 reconcile 足够）。
    6. **其余** → 订阅。phase=live + signal_allowed=True 表示"现在在打 + 可发信号"。
    """

    if has_exposure:
        return True

    # 显式 block：人工/自动 pause、已结算/已关闭，明确不该订阅。
    trading_status = getattr(market, "trading_status", None)
    status_value = getattr(trading_status, "value", trading_status)
    if status_value in {"paused", "rejected", "closed", "resolved"}:
        return False

    if entry_metadata is None:
        return False

    signal_allowed = getattr(entry_metadata, "live_state_signal_allowed", None)
    metadata = getattr(entry_metadata, "metadata", None)
    has_season_odds = isinstance(metadata, dict) and "season_odds_snapshot" in metadata

    # OUTRIGHT 路径：metadata 有 season_odds_snapshot → 订阅来捕获定价变动，
    # 即便 signal_allowed=False（live_state_worker 对赛季级市场不发"在打"信号）。
    if has_season_odds:
        return True

    # 非 OUTRIGHT：要求 signal_allowed=True（live_state_worker 明确放行）+
    # phase=live（确实在打）。signal_allowed=None / False 一律拒绝——表示
    # worker 还未对该 market 完成判断或显式拒绝。
    if signal_allowed is not True:
        return False
    phase = (getattr(entry_metadata, "live_state_phase", "") or "").strip().lower()
    return phase == "live"


def _offer_to_ws_queue(
    queue: asyncio.Queue[Mapping[str, Any]],
    payload: dict[str, Any],
    *,
    runtime: Any,
    worker_name: str,
) -> None:
    """非阻塞投递 WS payload。

    满载时丢弃最老一条腾位（ring buffer），并把 worker 标记为 DEGRADED 暴露背压；
    若仍满则放弃当前 payload。WS 摄入循环必须保持非阻塞，否则会反向阻塞 §7 P0 路径。
    """
    # 暴露 queue 大小到 SystemPerfMonitor 供 operator 查询
    try:
        from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
        SystemPerfMonitor.get().report_ws_queue(worker_name, queue.qsize(), queue.maxsize)
    except Exception:
        pass

    try:
        queue.put_nowait(payload)
        return
    except asyncio.QueueFull:
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            SystemPerfMonitor.get().record_ws_error(f"{worker_name}_queue_full")
        except Exception:
            pass
        pass
    with suppress(asyncio.QueueEmpty):
        queue.get_nowait()
    dropped_after_retry = False
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        dropped_after_retry = True
    runtime.supervisor.heartbeat_worker(
        worker_name,
        state=WorkerLifecycleState.DEGRADED,
        healthy=False,
        detail=f"queue_saturated qsize={queue.qsize()} dropped_new={dropped_after_retry}",
        last_error="ws_queue_saturated",
    )
    logger.warning(
        "ws queue saturated; dropped oldest payload",
        extra={
            "worker": worker_name,
            "qsize": queue.qsize(),
            "dropped_new": dropped_after_retry,
        },
    )


def market_ws_subscription_token_ids(runtime: Any) -> tuple[str, ...]:
    """返回 market WS 需要订阅的 token。

    统一通过 :func:`should_subscribe_ws` gate 决定——实时读 entry_metadata 的
    live_state_phase / signal_allowed + registry 的 trading_status + 账户 exposure。
    不再依赖任何"flag 类型"的中间状态，避免 stale 卡住的问题。
    超 _MARKET_WS_MAX_SUBSCRIPTIONS 时按 end_date 升序截断（最快结束的优先）。
    """

    account_snapshot = _account_snapshot(runtime)
    exposed_condition_ids, exposed_token_ids = _account_exposure_keys(account_snapshot)
    market_metadata_store = getattr(runtime, "market_metadata_store", None)
    candidates: list[tuple[float, tuple[str, ...]]] = []
    for market in runtime.registry.snapshot().markets:
        has_exposure = (
            market.condition_id in exposed_condition_ids
            or any(tid in exposed_token_ids for tid in market.token_ids)
        )
        entry = (
            market_metadata_store.find(condition_id=market.condition_id)
            if market_metadata_store is not None
            else None
        )
        if not should_subscribe_ws(market, entry, has_exposure):
            continue
        market_token_ids = tuple(tid for tid in market.token_ids if tid)
        if not market_token_ids:
            continue
        end_ts = float("inf")
        end = market.end_date
        if end is not None:
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            end_ts = end.timestamp()
        candidates.append((end_ts, market_token_ids))
    candidates.sort(key=lambda item: item[0])

    token_ids: list[str] = []
    seen: set[str] = set()
    for _, market_token_ids in candidates:
        for token_id in market_token_ids:
            if token_id in seen:
                continue
            seen.add(token_id)
            token_ids.append(token_id)
            if len(token_ids) >= _MARKET_WS_MAX_SUBSCRIPTIONS:
                break
        if len(token_ids) >= _MARKET_WS_MAX_SUBSCRIPTIONS:
            break
    return tuple(token_ids)


def _account_snapshot(runtime: RuntimeComponents) -> AccountSnapshot | None:
    store = runtime.account_state_store
    return store.snapshot() if store is not None else None


def _account_exposure_keys(account_snapshot: AccountSnapshot | None) -> tuple[set[str], set[str]]:
    condition_ids: set[str] = set()
    token_ids: set[str] = set()
    if account_snapshot is None:
        return condition_ids, token_ids
    for position in account_snapshot.positions:
        if position.settled_zero_value:
            continue
        if position.condition_id:
            condition_ids.add(position.condition_id)
        if position.token_id:
            token_ids.add(position.token_id)
    for order in account_snapshot.open_orders:
        if order.condition_id:
            condition_ids.add(order.condition_id)
        if order.token_id:
            token_ids.add(order.token_id)
    return condition_ids, token_ids



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


def market_ws_priority_token_ids(runtime: Any) -> frozenset[str]:
    """P3.2：返回当前持仓/挂单对应的 token_id 集合（最高优先级订阅）。

    这些 token 有资金暴露，订阅集变化时必须立即重建连接，
    不受 _MARKET_WS_RESUBSCRIBE_THRESHOLD 限制。
    """

    account_snapshot = _account_snapshot(runtime)
    _, exposed_token_ids = _account_exposure_keys(account_snapshot)
    return frozenset(exposed_token_ids)


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


def _trigger_reconcile_after_user_ws_connect(runtime: RuntimeComponents) -> None:
    """User WS 恢复后立即唤醒账户 reconcile，避免等待下一次周期调度。"""

    with suppress(KeyError):
        runtime.scheduler.trigger_now("periodic_reconcile")


async def stream_market_ws_messages(
    runtime: Any,
    token_ids: tuple[str, ...],
    queue: asyncio.Queue[Mapping[str, Any]],
) -> None:
    async def on_connect(attempt: int) -> None:
        runtime.market_ws_worker.clear_error()
        runtime.market_ws_worker.set_connection_state(True)
        runtime.supervisor.heartbeat_worker(
            "market_ws",
            state=WorkerLifecycleState.RUNNING,
            healthy=True,
            detail=f"connected subscribed={len(token_ids)} attempt={attempt}",
        )
        sync_runtime_metrics(runtime)

    async def on_disconnect(attempt: int) -> None:
        runtime.market_ws_worker.set_connection_state(False)
        runtime.supervisor.heartbeat_worker(
            "market_ws",
            state=WorkerLifecycleState.PAUSED,
            healthy=True,
            detail=f"disconnected attempt={attempt}",
        )
        sync_runtime_metrics(runtime)

    async def on_reconnect(attempt: int, exc: Exception) -> None:
        runtime.market_ws_worker.record_error(str(exc))
        runtime.market_ws_worker.set_connection_state(False)
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
        _offer_to_ws_queue(queue, dict(payload), runtime=runtime, worker_name="market_ws")


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


# 事件驱动退出：持仓 token 的盘口一更新就立刻唤醒 reconcile，让动态
# decide_exit 立即重估退出——比等下一次定时 reconcile 轮询快得多。
# 去抖窗口：盘口推送极频繁，限制最多每 0.5s 触发一次，避免 reconcile 被打爆。
_WS_EXIT_RECONCILE_DEBOUNCE_S = 0.5
_last_ws_exit_reconcile_at: float = 0.0


def _trigger_exit_reconcile_for_position_books(runtime: Any, events: Any) -> None:
    """有持仓的 token 收到盘口更新事件时，立即唤醒 periodic_reconcile。

    reconcile 每周期对每个持仓调用动态 ``decide_exit``；这里把"等定时轮询"
    变成"持仓盘口一动就触发"，退出能在价格变动后 ~0.5s 内重估（事件驱动）。
    """

    if not events:
        return
    store = getattr(runtime, "account_state_store", None)
    scheduler = getattr(runtime, "scheduler", None)
    if store is None or scheduler is None:
        return
    global _last_ws_exit_reconcile_at
    now = asyncio.get_running_loop().time()
    if now - _last_ws_exit_reconcile_at < _WS_EXIT_RECONCILE_DEBOUNCE_S:
        return
    snapshot = store.snapshot()
    for event in events:
        if getattr(event, "event_type", None) != DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED:
            continue
        condition_id = getattr(event, "condition_id", None)
        token_id = getattr(event, "token_id", None)
        if condition_id and token_id and snapshot.get_position(condition_id, token_id) is not None:
            _last_ws_exit_reconcile_at = now
            with suppress(KeyError):
                scheduler.trigger_now("periodic_reconcile")
            return


async def handle_market_ws_message(
    runtime: Any,
    message: Mapping[str, Any],
) -> None:
    if market_ws_message_type(message) == "new_market":
        await runtime.market_discovery_worker.ingest_ws_new_market(
            message,
            trace_id=f"market-ws-discovery-{uuid4().hex}",
        )
    events = await runtime.market_ws_worker.handle_message(message, source="market_ws")
    _trigger_exit_reconcile_for_position_books(runtime, events)


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
        _offer_to_ws_queue(queue, dict(payload), runtime=runtime, worker_name="user_ws")


async def run_market_ws(runtime: Any) -> None:
    # maxsize 4096 = 上下文:每条 orderbook 推送几百 bytes,4096 entries ~2MB 内存。
    # 此前 512 实测在 968 token 订阅下 ~30 次/秒 drop oldest(spam log);改 4096
    # 给 8× 缓冲,突发流量进队列而非立即 drop。trader 消费跟上后清空,无残留风险。
    queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=4096)
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
                # 已有运行中 stream task 时，只有变化量超过阈值才重建连接，避免
                # market_discovery 持续发现新市场导致握手永远无法完成。
                subscribed_set = set(subscribed_token_ids)
                desired_set = set(desired_token_ids)
                changed_token_ids = desired_set.symmetric_difference(subscribed_set)
                delta = len(changed_token_ids)
                # P3.2：有持仓/挂单的 priority token 发生变化时立即重建连接，
                # 不受 _MARKET_WS_RESUBSCRIBE_THRESHOLD 限制。
                # 资金已暴露的市场失去实时盘口更新会直接影响交易决策质量。
                priority_token_ids = market_ws_priority_token_ids(runtime)
                priority_delta = bool(changed_token_ids & priority_token_ids)
                needs_reconnect = (
                    desired_token_ids != subscribed_token_ids
                    and (stream_task is None or priority_delta or delta > _MARKET_WS_RESUBSCRIBE_THRESHOLD)
                )
                if needs_reconnect:
                    await _cancel_task(stream_task)
                    stream_task = None
                    subscribed_token_ids = ()
                    _drain_queue(queue)
                    if desired_token_ids:
                        runtime.market_ws_worker.build_subscription_request(desired_token_ids)
                        # 不再做 REST prefetch——Polymarket WS 订阅后会自动推一条
                        # 完整 book 消息（~1s 内）。原 prefetch 是"头几秒决策饥饿"
                        # 的过度防御，但下游 readiness gate (reconcile_fresh) 本来
                        # 就要等更久才允许下单，prefetch 节省的 1s 没业务价值。
                        # 唯一保留的 REST 路径：worker.py 内 sequence_gap 兜底。
                        next_subscription_refresh_at = loop.time() + _SUBSCRIPTION_REFRESH_SECONDS
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
                # delta <= threshold：不重建连接、不预取 REST。新增 token 由 WS
                # 订阅后下一条 book 消息（~1s）自然带快照。决策层在 orderbook
                # 缺数据时本来就走 missing_price_or_prob 跳过，无业务风险。

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
    queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=4096)
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
