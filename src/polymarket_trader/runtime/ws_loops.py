from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
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
_MARKET_WS_LIVE_STATUSES = {"live", "ended"}
_MARKET_WS_TAIL_WINDOW_SECONDS = 3600.0
# market WS 盘口订阅的生命周期窗口（基于 end_date≈game_start_time）：
#   - 开赛前 30 分钟内才开始订阅（PREGAME_LEAD）——不预订几小时/几天后的赛事；
#   - 开赛后 6h 内保持订阅（INPLAY_GRACE，覆盖各运动比赛全程）；
#   - 更早 / 更晚都不占订阅名额。
# 有持仓/挂单的市场在调用侧已提前放行，不受此窗口限制。
_MARKET_WS_PREGAME_LEAD_SECONDS = 1_800.0   # 开赛前 30 分钟
_MARKET_WS_INPLAY_GRACE_SECONDS = 21_600.0  # 开赛后 6 小时（覆盖比赛全程）
# 订阅数上限（按 token 计）——纯安全护栏，防止失控时把全量 registry 压垮
# Polymarket WS。订阅集已由 market_outside_trade_window（只跟踪开赛 [-6h,+30min]
# 的近期赛事）+ _market_requires_market_ws（live/敞口/6h 窗口）双重收窄，实际
# 近期相关市场 token 数典型 1500–4000；美国黄金时段多联赛叠加也远低于此上限。
# 旧值 200 会把 ~7/8 的直播市场截断成 missing_best_ask、卡掉成交机会（违反
# §17）。8000 给足余量，且远低于实测可用的 ~15000 token。超出时按 end_date
# 升序截断（最快结束 / 正在直播的优先保留）。
_MARKET_WS_MAX_SUBSCRIPTIONS = 8000
# stream task 已启动后容忍订阅集的小幅变化，避免持续 cancel/reconnect。
# 只有 added+removed > 此阈值才重建连接；新增 token 会在下次 reconnect 时补充。
_MARKET_WS_RESUBSCRIBE_THRESHOLD = 10


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

    try:
        queue.put_nowait(payload)
        return
    except asyncio.QueueFull:
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def market_ws_subscription_token_ids(runtime: Any) -> tuple[str, ...]:
    """返回 market WS 需要订阅的 token。

    全量 market 发现负责扩大机会池；market WS 只承载交易热路径所需盘口。
    收窄订阅范围避免把全量 registry 压到 Polymarket WS 限流。订阅条件：
    1. 账户已有敞口
    2. sports_live_state 已标记 live/ended（外部源覆盖时优先）
    3. **Polymarket 自身 active+open + endDate 在 6h 窗口内**（兜底——
       SofaScore 屏蔽 / ESPN 不覆盖 Challenger 时仍能订阅）

    超 _MARKET_WS_MAX_SUBSCRIPTIONS 时按 end_date 升序截断（最近结束的优先）。
    """

    account_snapshot = _account_snapshot(runtime)
    exposed_condition_ids, exposed_token_ids = _account_exposure_keys(account_snapshot)
    now = _utc_now()
    candidates: list[tuple[float, tuple[str, ...]]] = []
    for market in runtime.registry.snapshot().markets:
        if not _market_requires_market_ws(
            runtime,
            market,
            exposed_condition_ids=exposed_condition_ids,
            exposed_token_ids=exposed_token_ids,
            now=now,
        ):
            continue
        market_token_ids = tuple(token_id for token_id in market.token_ids if token_id)
        if not market_token_ids:
            continue
        # 按 end_date 升序排（near-end 优先）；缺 end_date 排到最后。
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


def _market_requires_market_ws(
    runtime: Any,
    market: Any,
    *,
    exposed_condition_ids: set[str],
    exposed_token_ids: set[str],
    now: datetime | None = None,
) -> bool:
    if market.condition_id in exposed_condition_ids or any(
        token_id in exposed_token_ids for token_id in market.token_ids
    ):
        return True
    now = now or _utc_now()
    record = _entry_metadata_record_for_market(runtime, market)
    if record is not None:
        # OUTRIGHT/SERIES 由专用 worker 写入 series_state/game_odds/season_odds_snapshot，
        # 不依赖 live_state——先判，避免误写的 signal_allowed=False 永久封锁这类市场
        # （市场重新分类时旧 False 记录会残留）。
        metadata = record.metadata or {}
        if metadata.get("series_state") or metadata.get("game_odds") or metadata.get("season_odds_snapshot"):
            # OUTRIGHT/SERIES 是长期市场，没有单场"开赛时刻"——不套单场赛事
            # 时间窗口，只要仍 ELIGIBLE 就订阅。
            return _market_eligible_for_ws(market)
        # 以下逻辑针对依赖 live_state 的市场（SINGLE_GAME）：
        # 显式拒（signal_allowed=False）立刻返回，避免 polymarket 兜底误绕过。
        if record.live_state_signal_allowed is False:
            return False
        phase = (record.live_state_phase or "").strip().lower()
        if phase == "ended":
            return True
        if phase in _MARKET_WS_LIVE_STATUSES:
            if record.live_state_signal_allowed is True:
                return True
            if _market_end_within_tail_window(market, now=now):
                return True
        # record 存在但仍是 scheduled 等未开赛态：按订阅时间窗口判定——
        # 开赛前 30 分钟内才订阅，更早不预订。
        return _market_active_in_polymarket(market, now=now)
    # record 不存在 = 外部 live state 没覆盖（典型：ATP Challenger / WTA 125 / ITF
    # 这些 ESPN 不收录、SofaScore 又被 Cloudflare 403 屏蔽的冷门赛事）。
    # 用 Polymarket 自身 ELIGIBLE + 订阅时间窗口作为兜底订阅信号。
    return _market_active_in_polymarket(market, now=now)


def _market_eligible_for_ws(market: Any) -> bool:
    """market 是否 ELIGIBLE（可交易）。

    domain ``Market`` 没有 active/closed 字段，权威判 ``trading_status``：
    只有 ELIGIBLE 才考虑订阅；CANDIDATE / PAUSED / CLOSED / RESOLVED / REJECTED 跳过。
    """

    from polymarket_trader.domain.market import TradingStatus  # 避免循环导入

    return market.trading_status == TradingStatus.ELIGIBLE


def _market_active_in_polymarket(market: Any, *, now: datetime) -> bool:
    """单场赛事市场是否在 WS 订阅时间窗口内（开赛前 30 分钟 ~ 开赛后 6h）。"""

    if not _market_eligible_for_ws(market):
        return False
    end = market.end_date
    if end is None:
        # 无 end_date 通常是赛季级 outright 市场——长期不订阅 WS 避免占用名额。
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    seconds_until_end = (end.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    # 只在"开赛前 30 分钟"到"开赛后 6h"窗口内订阅 WS 盘口（end_date≈game_start_time）：
    # 远期赛事不预订、早已结束的赛事不续订——这就是订阅的生命周期。
    return (
        -_MARKET_WS_INPLAY_GRACE_SECONDS
        <= seconds_until_end
        <= _MARKET_WS_PREGAME_LEAD_SECONDS
    )


def _market_end_within_tail_window(market: Any, *, now: datetime) -> bool:
    """判断 live market 是否进入实时盘口订阅窗口。

    全量扫描仍保留远期市场；这里只保护 market WS 热路径。已有持仓或挂单在
    调用侧已提前放行，ended 未封盘市场也不受该窗口限制。
    """

    market_end = market.end_date
    if market_end is None:
        return True
    if market_end.tzinfo is None:
        market_end = market_end.replace(tzinfo=timezone.utc)
    seconds_until_end = (market_end.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    return seconds_until_end <= _MARKET_WS_TAIL_WINDOW_SECONDS


def _entry_metadata_record_for_market(runtime: RuntimeComponents, market: Any) -> Any:
    """读取 entry metadata 强类型记录；缺失时返回 None。"""

    return runtime.entry_metadata_store.find(
        condition_id=market.condition_id,
        market_slug=market.market_slug,
        event_slug=market.event_slug,
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
                        await runtime.market_ws_worker.refresh_rest_snapshots(desired_token_ids)
                        # REST 预取可能耗时（并发 20 路仍需若干秒）。完成后重置定时器，
                        # 避免耗时结束时 next_subscription_refresh_at 已过期、下一轮循环
                        # 立即重建连接（stream_task 刚建好即被 cancel）。
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
                else:
                    # delta <= threshold：不重建连接，但仍为新增 token 预取 REST 快照，
                    # 避免它们在首条 WS 消息到达前以空盘口触发 missing_price_or_prob。
                    new_token_ids = tuple(desired_set - subscribed_set)
                    if new_token_ids:
                        await runtime.market_ws_worker.refresh_rest_snapshots(new_token_ids)

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
