from __future__ import annotations

import asyncio
import inspect
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any, Mapping

from polymarket_trader.domain.events import DomainEventType, OutboxEvent, sanitize_raw_response
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    ExecutionTimestamps,
    ManagedOrderIntent,
    OrderIntent,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderType,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.infra.polymarket.order_execution_types import (
    OrderExecutionClient,
    OrderExecutionRequest,
    OrderExecutionResponse,
)
from polymarket_trader.infra.polymarket.order_result_builder import (
    build_order_result,
    normalize_execution_response,
)
from polymarket_trader.infra.outbox.event_sink import OutboxSink
from polymarket_trader.serialization import utc_now

logger = logging.getLogger(__name__)


def _as_text(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _summarize_response(response: Any | None, *, max_length: int = 512) -> str | None:
    return sanitize_raw_response(response, max_length=max_length)


def _request_idempotency_key(
    *,
    action: str,
    trace_id: str,
    condition_id: str,
    token_id: str,
    side: OrderSide | None = None,
    order_type: OrderType | None = None,
    price: Decimal | None = None,
    amount_usdc: Decimal | None = None,
    size_shares: Decimal | None = None,
    market_slug: str | None = None,  # kept for call-site compat, not used in key
    order_id: str | None = None,
    new_price: Decimal | None = None,
    post_only: bool = False,
    reason: str = "",
    retry_count: int = 0,
    idempotency_key: str | None = None,
) -> str:
    if idempotency_key:
        return idempotency_key
    parts = [
        action,
        trace_id,
        condition_id,
        token_id,
        side.value if side is not None else "",
        order_type.value if order_type is not None else "",
        "" if price is None else str(price),
        "" if amount_usdc is None else str(amount_usdc),
        "" if size_shares is None else str(size_shares),
        order_id or "",
        "" if new_price is None else str(new_price),
        str(int(post_only)),
    ]
    return ":".join(parts)


@dataclass(frozen=True, slots=True)
class _IdempotencyEntry:
    signature: str
    task: asyncio.Task[OrderResult] | None = None
    result: OrderResult | None = None


class InMemoryPolymarketOrderClient:
    """Fake adapter for local development or tests."""

    def __init__(
        self,
        *,
        submit_handler: Any | None = None,
        cancel_handler: Any | None = None,
        replace_handler: Any | None = None,
        sign_handler: Any | None = None,
    ) -> None:
        self.requests: list[tuple[str, OrderExecutionRequest]] = []
        self._submit_handler = submit_handler
        self._cancel_handler = cancel_handler
        self._replace_handler = replace_handler
        self._sign_handler = sign_handler

    async def sign_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        self.requests.append(("sign", request))
        if callable(self._sign_handler):
            return await _maybe_await(self._sign_handler(request))
        return OrderExecutionResponse(
            status=OrderResultStatus.LIVE,
            raw_response={"signed": True, "request": request.action},
            reason="signed",
        )

    async def submit_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        self.requests.append(("submit", request))
        if callable(self._submit_handler):
            return await _maybe_await(self._submit_handler(request))
        if request.action == "submit" and request.side == OrderSide.BUY:
            if request.order_type == OrderType.FAK:
                return OrderExecutionResponse(
                    status=OrderResultStatus.NO_FILL,
                    order_id=f"fake-{request.idempotency_key}",
                    reason="simulated_fak_no_fill",
                    raw_response={"action": request.action, "status": "no_fill"},
                )
            return OrderExecutionResponse(
                status=OrderResultStatus.LIVE,
                order_id=f"fake-{request.idempotency_key}",
                reason="simulated_live_buy",
                raw_response={"action": request.action, "status": "live"},
            )
        if request.action == "submit" and request.side == OrderSide.SELL:
            return OrderExecutionResponse(
                status=OrderResultStatus.LIVE,
                order_id=f"fake-{request.idempotency_key}",
                reason="simulated_live_sell",
                raw_response={"action": request.action, "status": "live"},
            )
        return OrderExecutionResponse(
            status=OrderResultStatus.LIVE,
            order_id=f"fake-{request.idempotency_key}",
            reason="simulated_live",
            raw_response={"action": request.action, "status": "live"},
        )

    async def cancel_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        self.requests.append(("cancel", request))
        if callable(self._cancel_handler):
            return await _maybe_await(self._cancel_handler(request))
        return OrderExecutionResponse(
            status=OrderResultStatus.CANCELLED,
            order_id=request.order_id,
            reason="cancelled",
            raw_response={"action": request.action, "status": "cancelled"},
        )

    async def replace_order(self, request: OrderExecutionRequest) -> OrderExecutionResponse:
        self.requests.append(("replace", request))
        if callable(self._replace_handler):
            return await _maybe_await(self._replace_handler(request))
        return OrderExecutionResponse(
            status=OrderResultStatus.LIVE,
            order_id=f"fake-{request.idempotency_key}",
            reason="replaced",
            raw_response={"action": request.action, "status": "live"},
        )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class PolymarketOrderExecutor:
    """Only adapter allowed to submit, cancel, or replace Polymarket orders."""

    def __init__(
        self,
        *,
        client: OrderExecutionClient | None = None,
        outbox: OutboxSink | None = None,
        thread_pool: ThreadPoolExecutor | None = None,
        sign_timeout_ms: int = 1000,
        submit_timeout_ms: int = 3000,
        cancel_timeout_ms: int | None = None,
        replace_timeout_ms: int | None = None,
        critical_lock_timeout_ms: int = 20,
        max_cached_results: int = 1024,
    ) -> None:
        self._client = client
        self._outbox = outbox
        self._thread_pool = thread_pool or ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="trader-order-executor",
        )
        self._sign_timeout_s = sign_timeout_ms / 1000
        self._submit_timeout_s = submit_timeout_ms / 1000
        self._cancel_timeout_s = (cancel_timeout_ms or submit_timeout_ms) / 1000
        self._replace_timeout_s = (replace_timeout_ms or submit_timeout_ms) / 1000
        self._critical_lock_timeout_s = critical_lock_timeout_ms / 1000
        self._max_cached_results = max_cached_results
        self._idempotency_lock = asyncio.Lock()
        self._idempotency_index: dict[str, _IdempotencyEntry] = {}
        self._completed: "OrderedDict[str, OrderResult]" = OrderedDict()
        self._inflight: dict[str, asyncio.Task[OrderResult]] = {}

    async def submit(self, intent: OrderIntent) -> OrderResult:
        if not isinstance(intent, (BuyOrderIntent, SellOrderIntent)):
            raise TypeError(f"unsupported submit intent type: {type(intent)!r}")
        request = self._build_submit_request(intent)
        return await self._execute(request, intent)

    async def cancel(self, intent: CancelOrderIntent) -> OrderResult:
        request = self._build_cancel_request(intent)
        return await self._execute(request, intent)

    async def replace(self, intent: ReplaceOrderIntent) -> OrderResult:
        request = self._build_replace_request(intent)
        return await self._execute(request, intent)

    async def aclose(self) -> None:
        # wait=True 确保正在进行的 EIP-712 签名任务完成后再关闭线程池，避免签名中断导致订单状态不确定。
        await asyncio.to_thread(self._thread_pool.shutdown, wait=True, cancel_futures=False)

    def close(self) -> None:
        # 同步等待签名任务完成，防止关闭时中断进行中的订单签名。
        self._thread_pool.shutdown(wait=True, cancel_futures=False)

    def _build_submit_request(self, intent: BuyOrderIntent | SellOrderIntent) -> OrderExecutionRequest:
        return OrderExecutionRequest(
            action="submit",
            strategy_id=intent.strategy_id,
            trace_id=intent.trace_id,
            idempotency_key=_request_idempotency_key(
                action="submit",
                trace_id=intent.trace_id,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                side=intent.side,
                order_type=intent.order_type,
                price=intent.price,
                amount_usdc=intent.amount_usdc,
                size_shares=intent.size_shares,
                market_slug=intent.market_slug,
                post_only=intent.post_only,
                retry_count=intent.retry_count,
                idempotency_key=intent.idempotency_key,
            ),
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            side=intent.side,
            order_type=intent.order_type,
            price=intent.price,
            amount_usdc=intent.amount_usdc,
            size_shares=intent.size_shares,
            post_only=intent.post_only,
            retry_count=intent.retry_count,
        )

    def _build_cancel_request(self, intent: CancelOrderIntent) -> OrderExecutionRequest:
        return OrderExecutionRequest(
            action="cancel",
            strategy_id=intent.strategy_id,
            trace_id=intent.trace_id,
            idempotency_key=_request_idempotency_key(
                action="cancel",
                trace_id=intent.trace_id,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                market_slug=intent.market_slug,
                order_id=intent.order_id,
                reason=intent.reason,
                idempotency_key=intent.idempotency_key,
            ),
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            order_id=intent.order_id,
            reason=intent.reason,
        )

    def _build_replace_request(self, intent: ReplaceOrderIntent) -> OrderExecutionRequest:
        return OrderExecutionRequest(
            action="replace",
            strategy_id=intent.strategy_id,
            trace_id=intent.trace_id,
            idempotency_key=_request_idempotency_key(
                action="replace",
                trace_id=intent.trace_id,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                market_slug=intent.market_slug,
                order_id=intent.order_id,
                new_price=intent.new_price,
                size_shares=intent.size_shares,
                reason=intent.reason,
                idempotency_key=intent.idempotency_key,
            ),
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            order_id=intent.order_id,
            new_price=intent.new_price,
            size_shares=intent.size_shares,
            reason=intent.reason,
        )

    async def _execute(
        self,
        request: OrderExecutionRequest,
        intent: ManagedOrderIntent,
    ) -> OrderResult:
        started_at = utc_now()
        request = replace(request, timestamps=ExecutionTimestamps(queued_at=started_at))
        signature = request.fingerprint()
        task: asyncio.Task[OrderResult] | None = None
        existing_result: OrderResult | None = None
        existing_task: asyncio.Task[OrderResult] | None = None
        try:
            lock = await self._acquire_lock()
        except TimeoutError:
            # 不变量：lock 超时意味着我们**根本没启动 task**——既没读也没写 idempotency_index。
            # `retryable=True` 是安全的：idempotency_key 派生包含 trace_id，正常重试用新 trace_id
            # → 新 key，不撞当前路径；同 key 重试只可能源于同一发起方，而那个发起方根本没启动 task，
            # 所以"重试时 index 找不到条目并重新启动"恰是预期，无双发风险。
            result = self._timeout_result(
                request,
                intent,
                started_at=started_at,
                reason="critical_lock_timeout",
            )
            asyncio.create_task(
                self._publish_lifecycle_event(
                    "order_state_updated",
                    request,
                    intent,
                    result=result,
                    reason=result.reason,
                    raw_response=result.raw_response_summary,
                    timestamps=result.timestamps,
                )
            )
            return result
        try:
            entry = self._idempotency_index.get(request.idempotency_key)
            if entry is not None:
                if entry.signature != signature:
                    existing_result = self._conflict_result(request, intent)
                elif entry.result is not None:
                    existing_result = entry.result
                elif entry.task is not None:
                    existing_task = entry.task
            if existing_result is None and existing_task is None:
                # 锁内只做三件同步操作：create_task（事件循环 schedule，不 await）、
                # dict 写、add_done_callback（O(1) 推入回调列表）。无 IO await、无日志、
                # 无大对象序列化——符合 §7 关键锁内禁止反向阻塞。
                task = asyncio.create_task(self._run_operation(request, intent))
                self._idempotency_index[request.idempotency_key] = _IdempotencyEntry(
                    signature=signature,
                    task=task,
                )
                self._inflight[request.idempotency_key] = task
                def _store_finished(finished: asyncio.Task[OrderResult]) -> None:
                    self._store_task_result(request.idempotency_key, finished)

                task.add_done_callback(_store_finished)
        finally:
            lock.release()

        if existing_result is not None:
            return existing_result
        if existing_task is not None:
            return await self._await_task(existing_task, request, intent)
        if task is None:
            raise RuntimeError(
                f"order_executor: task not created for idempotency_key={request.idempotency_key!r}"
            )
        return await self._await_task(task, request, intent)

    async def _await_task(
        self,
        task: asyncio.Task[OrderResult],
        request: OrderExecutionRequest,
        intent: ManagedOrderIntent,
    ) -> OrderResult:
        timeout_s = self._timeout_for_action(request.action)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except asyncio.TimeoutError:
            result = self._timeout_result(request, intent, started_at=request.timestamps.queued_at)
            asyncio.create_task(
                self._publish_lifecycle_event(
                    "order_state_updated",
                    request,
                    intent,
                    result=result,
                    reason=result.reason,
                    raw_response=result.raw_response_summary,
                    timestamps=result.timestamps,
                )
            )
            return result

    async def _run_operation(
        self,
        request: OrderExecutionRequest,
        intent: ManagedOrderIntent,
    ) -> OrderResult:
        timestamps = request.timestamps
        outbox_warning: str | None = None
        try:
            if request.action == "submit":
                await self._publish_lifecycle_event("order_created", request, intent, reason=request.reason)
            elif request.action == "cancel":
                await self._publish_lifecycle_event(
                    "order_cancel_requested",
                    request,
                    intent,
                    reason=request.reason,
                    order_id=request.order_id,
                )
            elif request.action == "replace":
                await self._publish_lifecycle_event(
                    "order_cancel_requested",
                    request,
                    intent,
                    reason=request.reason,
                    order_id=request.order_id,
                )

            if request.action in {"submit", "cancel", "replace"}:
                sign_started_at = utc_now()
                timestamps = ExecutionTimestamps(
                    queued_at=timestamps.queued_at,
                    sign_started_at=sign_started_at,
                )
                # OrderExecutionClient Protocol 声明 sign_order 必存在；client=None 时
                # 上游已在 _invoke_adapter 内 raise，这里仅判断"是否注入了 client"。
                signed_response = None
                if self._client is not None:
                    signed_response = await self._invoke_adapter(
                        "sign_order",
                        request,
                        timeout_s=self._sign_timeout_s,
                    )
                timestamps = ExecutionTimestamps(
                    queued_at=timestamps.queued_at,
                    sign_started_at=sign_started_at,
                    signed_at=utc_now(),
                )
                await self._publish_lifecycle_event(
                    "order_signed",
                    request,
                    intent,
                    reason=_response_reason(signed_response, fallback="signed"),
                    raw_response=_response_raw(signed_response)
                    if signed_response is not None
                    else {"signed": True, "inline": True},
                    timestamps=timestamps,
                )

            submit_started_at = utc_now()
            timestamps = ExecutionTimestamps(
                queued_at=timestamps.queued_at,
                sign_started_at=timestamps.sign_started_at,
                signed_at=timestamps.signed_at,
                submitted_at=submit_started_at,
            )
            response = await self._invoke_adapter(
                _adapter_method_for_action(request.action),
                request,
                timeout_s=self._timeout_for_action(request.action),
            )
            response_model = normalize_execution_response(
                response,
                action=request.action,
                intent=intent,
                timestamps=timestamps,
            )
            result = build_order_result(
                request=request,
                intent=intent,
                response=response_model,
                timestamps=ExecutionTimestamps(
                    queued_at=timestamps.queued_at,
                    sign_started_at=timestamps.sign_started_at,
                    signed_at=timestamps.signed_at,
                    submitted_at=timestamps.submitted_at,
                    ack_at=utc_now(),
                ),
            )
            event_type = _final_event_type(request.action, result.status)
            await self._publish_lifecycle_event(
                event_type,
                request,
                intent,
                result=result,
                reason=result.reason,
                raw_response=result.raw_response_summary,
                timestamps=result.timestamps,
            )
            await self._publish_lifecycle_event(
                "order_state_updated",
                request,
                intent,
                result=result,
                reason=result.reason,
                raw_response=result.raw_response_summary,
                timestamps=result.timestamps,
            )
            return result
        except asyncio.TimeoutError:
            result = self._timeout_result(request, intent, started_at=timestamps.queued_at)
            asyncio.create_task(
                self._publish_lifecycle_event(
                    "order_state_updated",
                    request,
                    intent,
                    result=result,
                    reason=result.reason,
                    raw_response=result.raw_response_summary,
                    timestamps=result.timestamps,
                )
            )
            return result
        except Exception as exc:
            return await self._failure_result(
                request,
                intent,
                reason=str(exc),
                retryable=False,
                timestamps=timestamps,
                outbox_warning=outbox_warning,
            )

    async def _failure_result(
        self,
        request: OrderExecutionRequest,
        intent: ManagedOrderIntent,
        *,
        reason: str,
        retryable: bool,
        timestamps: ExecutionTimestamps,
        outbox_warning: str | None = None,
    ) -> OrderResult:
        final_reason = reason if outbox_warning is None else f"{reason}; outbox={outbox_warning}"
        result = OrderResult(
            strategy_id=request.strategy_id,
            trace_id=request.trace_id,
            condition_id=request.condition_id,
            token_id=request.token_id,
            market_slug=request.market_slug,
            status=OrderResultStatus.FAILED,
            intent=intent,
            side=request.side,
            order_type=request.order_type,
            price=request.price or request.new_price,
            requested_amount_usdc=request.amount_usdc,
            requested_size_shares=request.size_shares,
            reason=final_reason,
            retryable=retryable,
            timestamps=ExecutionTimestamps(
                queued_at=timestamps.queued_at,
                sign_started_at=timestamps.sign_started_at,
                signed_at=timestamps.signed_at,
                submitted_at=timestamps.submitted_at,
                ack_at=utc_now(),
            ),
        )
        await self._publish_lifecycle_event(
            "order_rejected",
            request,
            intent,
            result=result,
            reason=result.reason,
            raw_response=result.raw_response_summary,
            timestamps=result.timestamps,
        )
        await self._publish_lifecycle_event(
            "order_state_updated",
            request,
            intent,
            result=result,
            reason=result.reason,
            raw_response=result.raw_response_summary,
            timestamps=result.timestamps,
        )
        return result

    def _timeout_result(
        self,
        request: OrderExecutionRequest,
        intent: ManagedOrderIntent,
        *,
        started_at: datetime | None,
        reason: str | None = None,
    ) -> OrderResult:
        return OrderResult(
            strategy_id=request.strategy_id,
            trace_id=request.trace_id,
            condition_id=request.condition_id,
            token_id=request.token_id,
            market_slug=request.market_slug,
            status=OrderResultStatus.UNKNOWN_TIMEOUT,
            intent=intent,
            side=request.side,
            order_type=request.order_type,
            price=request.price or request.new_price,
            requested_amount_usdc=request.amount_usdc,
            requested_size_shares=request.size_shares,
            reason=reason or f"{request.action}_timeout",
            retryable=True,
            timestamps=ExecutionTimestamps(
                queued_at=started_at,
                submitted_at=utc_now(),
                ack_at=utc_now(),
            ),
        )

    async def _publish_lifecycle_event(
        self,
        event_type: str,
        request: OrderExecutionRequest,
        intent: ManagedOrderIntent,
        *,
        reason: str = "",
        raw_response: Any | None = None,
        result: OrderResult | None = None,
        order_id: str | None = None,
        timestamps: ExecutionTimestamps | None = None,
    ) -> None:
        event = OutboxEvent(
            trace_id=request.trace_id,
            event_type=_coerce_event_type(event_type),
            idempotency_key=f"{request.idempotency_key}:{event_type}",
            market_slug=request.market_slug,
            condition_id=request.condition_id,
            token_id=request.token_id,
            reason=reason,
            priority=0,
            raw_response_summary=_summarize_response(raw_response),
            payload={
                "action": request.action,
                "idempotency_key": request.idempotency_key,
                "order_id": order_id or (None if result is None else result.order_id),
                "status": None if result is None else result.status.value,
                "side": None if request.side is None else request.side.value,
                "order_type": None if request.order_type is None else request.order_type.value,
                "price": None
                if (request.price is None and request.new_price is None)
                else str(request.price or request.new_price),
                "amount_usdc": None if request.amount_usdc is None else str(request.amount_usdc),
                "size_shares": None if request.size_shares is None else str(request.size_shares),
                "post_only": request.post_only,
                "new_price": None if request.new_price is None else str(request.new_price),
                "timestamps": _serialize_timestamps(timestamps or request.timestamps),
                "intent": _serialize_intent(intent),
            },
        )
        await self._enqueue_outbox(event)

    async def _enqueue_outbox(self, event: OutboxEvent) -> None:
        if self._outbox is None:
            return
        try:
            self._outbox.put_nowait(event)
        except Exception:
            # Outbox 失败不能挡住交易热路径；执行器仍然继续返回订单结果。
            logger.warning("order_executor.outbox_enqueue_failed", exc_info=True)
            return

    async def _invoke_adapter(
        self,
        method_name: str,
        request: OrderExecutionRequest,
        *,
        timeout_s: float,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("missing execution client")
        method = getattr(self._client, method_name, None)
        if method is None:
            raise AttributeError(f"execution client does not implement {method_name}")
        if inspect.iscoroutinefunction(method):
            return await asyncio.wait_for(method(request), timeout=timeout_s)
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(self._thread_pool, partial(method, request)),
            timeout=timeout_s,
        )

    async def _acquire_lock(self) -> asyncio.Lock:
        try:
            await asyncio.wait_for(self._idempotency_lock.acquire(), timeout=self._critical_lock_timeout_s)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("order executor critical lock timeout") from exc
        return self._idempotency_lock

    def _store_task_result(self, key: str, task: asyncio.Task[OrderResult]) -> None:
        entry = self._idempotency_index.get(key)
        if entry is None:
            return
        with suppress(Exception):
            result = task.result()
            self._idempotency_index[key] = _IdempotencyEntry(
                signature=entry.signature,
                task=None,
                result=result,
            )
            self._completed[key] = result
            self._completed.move_to_end(key)
            self._trim_completed_cache()
        self._inflight.pop(key, None)

    def _trim_completed_cache(self) -> None:
        while len(self._completed) > self._max_cached_results:
            key, _ = self._completed.popitem(last=False)
            # _idempotency_index 同步删除，防止长期运行时 task=None 的幽灵条目无限积累。
            # 缓存淘汰 = 同 idempotency_key 重发会重新执行而非返回缓存。这种"被动失效"
            # 对实盘资金是潜在风险（CLAUDE.md §3「禁止 resting BUY」就靠幂等防重复挂单）。
            # 记一条 WARNING 让运维知道压力上来了；正常稳态不会到 1024+ 在飞的订单。
            self._idempotency_index.pop(key, None)
            logger.warning(
                "order_executor: idempotency cache evicted key=%s (cap=%d, current=%d). "
                "Subsequent retries on this key will re-execute, not dedupe.",
                key,
                self._max_cached_results,
                len(self._completed),
            )

    def _conflict_result(
        self,
        request: OrderExecutionRequest,
        intent: ManagedOrderIntent,
    ) -> OrderResult:
        return OrderResult(
            strategy_id=request.strategy_id,
            trace_id=request.trace_id,
            condition_id=request.condition_id,
            token_id=request.token_id,
            market_slug=request.market_slug,
            status=OrderResultStatus.FAILED,
            intent=intent,
            side=request.side,
            order_type=request.order_type,
            price=request.price or request.new_price,
            requested_amount_usdc=request.amount_usdc,
            requested_size_shares=request.size_shares,
            reason="idempotency_key_conflict",
            retryable=False,
            timestamps=ExecutionTimestamps(queued_at=utc_now(), ack_at=utc_now()),
        )

    def _timeout_for_action(self, action: str) -> float:
        if action == "cancel":
            return self._cancel_timeout_s
        if action == "replace":
            return self._replace_timeout_s
        return self._submit_timeout_s


def _adapter_method_for_action(action: str) -> str:
    if action == "cancel":
        return "cancel_order"
    if action == "replace":
        return "replace_order"
    return "submit_order"


def _final_event_type(action: str, status: OrderResultStatus) -> str:
    if status in {
        OrderResultStatus.REJECTED,
        OrderResultStatus.FAILED,
        OrderResultStatus.UNKNOWN_TIMEOUT,
    }:
        return "order_rejected"
    if action == "cancel":
        return "order_cancelled"
    if action == "replace":
        return "replace_order_submitted"
    return "order_submitted"


def _coerce_event_type(event_type: str) -> str:
    try:
        return DomainEventType(event_type).value
    except ValueError:
        return event_type


def _response_reason(response: Any, *, fallback: str = "") -> str:
    if isinstance(response, OrderExecutionResponse):
        return response.reason or fallback
    reason = _as_text(getattr(response, "reason", None))
    return reason or fallback


def _response_raw(response: Any) -> Any:
    if isinstance(response, OrderExecutionResponse):
        return response.raw_response
    if isinstance(response, Mapping):
        return response
    return getattr(response, "raw_response", response)


def _serialize_intent(intent: ManagedOrderIntent) -> dict[str, Any]:
    data: dict[str, Any] = {
        "trace_id": intent.trace_id,
        "condition_id": intent.condition_id,
        "token_id": intent.token_id,
        "market_slug": intent.market_slug,
    }
    if isinstance(intent, BuyOrderIntent):
        data["side"] = intent.side.value
        data["order_type"] = intent.order_type.value
        data["price"] = str(intent.price)
        data["amount_usdc"] = str(intent.amount_usdc)
    elif isinstance(intent, SellOrderIntent):
        data["side"] = intent.side.value
        data["order_type"] = intent.order_type.value
        data["price"] = str(intent.price)
        data["size_shares"] = str(intent.size_shares)
    elif isinstance(intent, CancelOrderIntent):
        data["order_id"] = intent.order_id
    elif isinstance(intent, ReplaceOrderIntent):
        data["order_id"] = intent.order_id
        data["new_price"] = str(intent.new_price)
        data["size_shares"] = str(intent.size_shares)
    return data


def _serialize_timestamps(timestamps: ExecutionTimestamps) -> dict[str, str | None]:
    return {
        "queued_at": None if timestamps.queued_at is None else timestamps.queued_at.isoformat(),
        "sign_started_at": None
        if timestamps.sign_started_at is None
        else timestamps.sign_started_at.isoformat(),
        "signed_at": None if timestamps.signed_at is None else timestamps.signed_at.isoformat(),
        "submitted_at": None
        if timestamps.submitted_at is None
        else timestamps.submitted_at.isoformat(),
        "ack_at": None if timestamps.ack_at is None else timestamps.ack_at.isoformat(),
    }
