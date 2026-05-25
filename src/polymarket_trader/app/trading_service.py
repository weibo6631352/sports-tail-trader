from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Protocol, runtime_checkable
from uuid import uuid4

from polymarket_trader.domain.allocation import AllocationPlan
from polymarket_trader.domain.events import (
    DomainEvent,
    DomainEventType,
    OutboxPriority,
)
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    ManagedOrderIntent,
    Order,
    OrderIntent,
    OrderResult,
    OrderResultStatus,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.risk import RiskDecision, RiskManager
from polymarket_trader.contracts.lifecycle import LifecycleEvent
from polymarket_trader.runtime.lifecycle_bus import LifecyclePublisher

logger = logging.getLogger(__name__)


@runtime_checkable
class OrderExecutorProtocol(Protocol):
    async def submit(self, intent: OrderIntent) -> OrderResult: ...
    async def cancel(self, intent: CancelOrderIntent) -> OrderResult: ...
    async def replace(self, intent: ReplaceOrderIntent) -> OrderResult: ...


class TradingService:
    """Coordinates risk-checked order intents and execution."""

    def __init__(
        self,
        *,
        risk_manager: RiskManager | None = None,
        executor: OrderExecutorProtocol | None = None,
        lifecycle_bus: LifecyclePublisher | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self._risk_manager = risk_manager or RiskManager()
        self._executor = executor
        self._lifecycle_bus = lifecycle_bus
        # 风控拒绝结构化落库走 event_bus → outbox → audit_events。可选，旧测试
        # 不传也行；CLAUDE.md §3 要求 RiskManager 是强制门禁，但 §7 又要 P0 主
        # 链路只允许 put_nowait，所以投递失败不能反向阻塞 review_intent。
        self._event_bus = event_bus
        # 风控拒绝在 bus 不支持 publish_nowait 时降级为 fire-and-forget；保留强
        # 引用避免 asyncio 弱引用语义下任务被 GC 静默吞掉，导致审计事件丢失（§7）。
        # 没有专属 aclose 入口时，集合在 done_callback 中自然 discard。
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_background(self, coro: Any, *, name: str) -> asyncio.Task[Any]:
        """登记 fire-and-forget 任务到强引用集合，防止被 GC 中途吞掉。"""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_task_exception)
        return task

    def _log_task_exception(self, task: asyncio.Task[Any]) -> None:
        # 后台任务静默失败会让风控拒绝审计链路出现空洞——主动通过模块 logger 上报。
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        if isinstance(exc, asyncio.CancelledError):
            return
        logger.warning(
            "trading_service.background_task_failed name=%s",
            task.get_name(),
            exc_info=exc,
        )

    async def aclose(self) -> None:
        """drain fire-and-forget 任务，保证审计事件不在关停时丢失。"""
        pending = list(self._background_tasks)
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.clear()

    async def review_intent(
        self,
        intent: OrderIntent,
        *,
        market: Market | None = None,
        orderbook: OrderbookSnapshot | None = None,
        position: Position | None = None,
        open_orders: Iterable[Order] = (),
        condition_open_orders: Iterable[Order] = (),
        condition_positions: Iterable[Position] = (),
        allocation_plan: AllocationPlan | None = None,
        classification_passed: bool | None = True,
        classification_reason: str | None = None,
        market_active: bool | None = None,
        market_open: bool | None = None,
        clob_enabled: bool | None = None,
        resolved: bool | None = None,
        cancelled: bool | None = None,
        archived: bool | None = None,
        geoblocked: bool = False,
        balance_usdc: Decimal | None = None,
        allowance_usdc: Decimal | None = None,
        portfolio_total_invested_usdc: Decimal | None = None,
        open_orders_count: int | None = None,
        bankroll_usdc: Decimal | None = None,
        kelly_max_position_fraction: Decimal | None = None,
        kelly_round_up_max_overbet_ratio: Decimal | None = None,
        min_order_size: Decimal | None = None,
        operation: str = "review",
    ) -> "TradingReviewResult":
        risk_decision = self._risk_manager.check_order_intent(
            intent,
            market=market,
            orderbook=orderbook,
            position=position,
            open_orders=open_orders,
            condition_open_orders=condition_open_orders,
            condition_positions=condition_positions,
            allocation_plan=allocation_plan,
            classification_passed=classification_passed,
            classification_reason=classification_reason,
            market_active=market_active,
            market_open=market_open,
            clob_enabled=clob_enabled,
            resolved=resolved,
            cancelled=cancelled,
            archived=archived,
            geoblocked=geoblocked,
            balance_usdc=balance_usdc,
            allowance_usdc=allowance_usdc,
            portfolio_total_invested_usdc=portfolio_total_invested_usdc,
            open_orders_count=open_orders_count,
            bankroll_usdc=bankroll_usdc,
            kelly_max_position_fraction=kelly_max_position_fraction,
            kelly_round_up_max_overbet_ratio=kelly_round_up_max_overbet_ratio,
            min_order_size=min_order_size,
        )
        if risk_decision.passed:
            order_result, submitted, submission_error = await self._execute_trade_intent(
                intent,
                operation=operation,
            )
        else:
            order_result = _synthetic_order_result(
                intent,
                status=OrderResultStatus.REJECTED,
                reason=risk_decision.reason,
                retryable=risk_decision.retryable,
            )
            submitted = False
            submission_error = None
            # 风控拒绝结构化落库走 P3 outbox mirror，同步 publish_nowait 即可，
            # 绝不 await——审计落库不能反向阻塞 P0 主链路（§7）。
            self._publish_risk_rejection(
                intent=intent, risk_decision=risk_decision, operation=operation
            )
        self._publish_lifecycle(
            intent=intent,
            operation=operation,
            risk_decision=risk_decision,
            order_result=order_result,
        )
        return TradingReviewResult(
            intent=intent,
            operation=operation,
            risk_decision=risk_decision,
            submitted=submitted,
            order_result=order_result,
            submission_error=submission_error,
        )

    async def buy(self, intent: BuyOrderIntent, **kwargs: Any) -> "TradingReviewResult":
        return await self.review_intent(intent, operation="buy", **kwargs)

    async def sell(self, intent: SellOrderIntent, **kwargs: Any) -> "TradingReviewResult":
        return await self.review_intent(intent, operation="sell", **kwargs)

    async def cancel(
        self,
        intent: CancelOrderIntent,
        *,
        operation: str = "cancel",
    ) -> "TradingReviewResult":
        order_result, submitted, submission_error = await self._execute_control_intent(
            intent,
            operation=operation,
        )
        self._publish_lifecycle(
            intent=intent,
            operation=operation,
            risk_decision=None,
            order_result=order_result,
        )
        return TradingReviewResult(
            intent=intent,
            operation=operation,
            risk_decision=None,
            submitted=submitted,
            order_result=order_result,
            submission_error=submission_error,
        )

    async def replace(
        self,
        intent: ReplaceOrderIntent,
        *,
        operation: str = "replace",
    ) -> "TradingReviewResult":
        order_result, submitted, submission_error = await self._execute_control_intent(
            intent,
            operation=operation,
        )
        self._publish_lifecycle(
            intent=intent,
            operation=operation,
            risk_decision=None,
            order_result=order_result,
        )
        return TradingReviewResult(
            intent=intent,
            operation=operation,
            risk_decision=None,
            submitted=submitted,
            order_result=order_result,
            submission_error=submission_error,
        )

    def _publish_risk_rejection(
        self,
        *,
        intent: ManagedOrderIntent,
        risk_decision: RiskDecision,
        operation: str,
    ) -> None:
        """风控拒绝结构化事件——同步 fire-and-forget，绝不 await。

        ``event_bus`` 缺失或 publish_nowait 抛错都静默：P0 主链路已判定完成，
        审计落库属于副作用，不能反向阻塞（§7）。``checks[].value`` 在源对象上
        可能是 Position / list / Decimal 等任意对象，``str()`` 化属于"在主链路
        同步写大 payload"——故只保留可审计的 ``name/passed/reason/field/
        suggested_action/retryable``，丢弃 value 字段。
        """

        bus = self._event_bus
        if bus is None:
            return
        checks_payload = [
            {
                "name": check.name,
                "passed": check.passed,
                "reason": check.reason,
                "field": check.field,
                "suggested_action": check.suggested_action,
                "retryable": check.retryable,
            }
            for check in (risk_decision.checks or ())
        ]
        # Buy/Sell intent 携带 side enum；Cancel/Replace 不携带——用 isinstance narrow
        # 替代 hasattr+getattr duck-typing。
        intent_summary: dict[str, object] = {"operation": operation, "side": None}
        if isinstance(intent, (BuyOrderIntent, SellOrderIntent)):
            intent_summary["side"] = intent.side.value
        event = DomainEvent(
            trace_id=intent.trace_id,
            event_type=DomainEventType.RISK_REJECTION_RECORDED,
            event_id=uuid4().hex,
            market_slug=intent.market_slug,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            reason=risk_decision.reason or "risk_rejected",
            payload={
                "passed": False,
                "reason": risk_decision.reason,
                "failed_field": risk_decision.failed_field,
                "checks": checks_payload,
                "intent_summary": intent_summary,
                "decision_kind": operation,
            },
        )
        try:
            bus.publish_nowait(OutboxPriority.P3, event)
        except AttributeError:
            # event_bus 不支持 publish_nowait（如测试桩）——降级为 fire-and-forget task。
            # 必须保留强引用：Python asyncio 文档明确警告未保留引用的 task 可能在
            # 执行中被 GC，从而静默吞掉风控拒绝审计事件（§7）。
            self._spawn_background(
                bus.publish(OutboxPriority.P3, event),
                name="trading_service.publish_risk_event",
            )
        except Exception:
            # 审计事件投递失败必须可见，但不能阻塞 P0 主链路（§7）。
            logger.warning(
                "trading_service.publish_risk_event_failed",
                exc_info=True,
                extra={"trace_id": intent.trace_id, "condition_id": intent.condition_id},
            )
            return

    def _publish_lifecycle(
        self,
        *,
        intent: ManagedOrderIntent,
        operation: str,
        risk_decision: RiskDecision | None,
        order_result: OrderResult | None,
    ) -> None:
        """把订单结果转译为 lifecycle 事件供策略订阅。

        映射按 ``operation`` 优先：cancel/replace 操作只发 ORDER_CANCELLED 或
        ORDER_REJECTED，避免在 cancel 路径里发出 ORDER_SUBMITTED / ORDER_FILLED 误导策略。
        NO_FILL / UNKNOWN_TIMEOUT 这种"等待状态"不产生事件，避免给策略噪音。
        """

        if self._lifecycle_bus is None:
            return
        status = order_result.status if order_result is not None else None
        if operation in {"cancel", "replace"}:
            if status in (OrderResultStatus.CANCELLED, OrderResultStatus.FULL_FILL, OrderResultStatus.PARTIAL_FILL):
                event = LifecycleEvent.ORDER_CANCELLED
            elif status in (OrderResultStatus.REJECTED, OrderResultStatus.FAILED):
                event = LifecycleEvent.ORDER_REJECTED
            else:
                return
        elif risk_decision is not None and not risk_decision.passed:
            event = LifecycleEvent.ORDER_REJECTED
        else:
            if status in (OrderResultStatus.FULL_FILL, OrderResultStatus.PARTIAL_FILL):
                event = LifecycleEvent.ORDER_FILLED
            elif status == OrderResultStatus.LIVE:
                event = LifecycleEvent.ORDER_SUBMITTED
            elif status == OrderResultStatus.CANCELLED:
                event = LifecycleEvent.ORDER_CANCELLED
            elif status in (OrderResultStatus.REJECTED, OrderResultStatus.FAILED):
                event = LifecycleEvent.ORDER_REJECTED
            else:
                return
        payload: dict[str, Any] = {"operation": operation}
        if order_result is not None:
            payload.update(
                {
                    "status": order_result.status.value,
                    "order_id": order_result.order_id,
                    "trade_id": order_result.trade_id,
                    "matched_shares": str(order_result.matched_shares),
                    "spent_usdc": str(order_result.spent_usdc),
                    "reason": order_result.reason,
                    "retryable": order_result.retryable,
                }
            )
        intent_tags = intent.intent_tags
        if intent_tags:
            payload["intent_tags"] = tuple(sorted(intent_tags))
        self._lifecycle_bus.publish(
            event,
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            payload=payload,
        )

    async def _execute_trade_intent(
        self,
        intent: OrderIntent,
        *,
        operation: str,
    ) -> tuple[OrderResult, bool, str | None]:
        if self._executor is None:
            return (
                _synthetic_order_result(intent, status=OrderResultStatus.FAILED, reason="executor_unavailable", retryable=True),
                False,
                None,
            )
        try:
            order_result = await self._executor.submit(intent)
            return order_result, True, None
        except Exception as exc:
            error = str(exc)
            return (
                _synthetic_order_result(intent, status=OrderResultStatus.FAILED, reason=error, retryable=True),
                True,
                error,
            )

    async def _execute_control_intent(
        self,
        intent: CancelOrderIntent | ReplaceOrderIntent,
        *,
        operation: str,
    ) -> tuple[OrderResult, bool, str | None]:
        if self._executor is None:
            return (
                _synthetic_order_result(intent, status=OrderResultStatus.FAILED, reason="executor_unavailable", retryable=True),
                False,
                None,
            )
        try:
            if isinstance(intent, CancelOrderIntent):
                order_result = await self._executor.cancel(intent)
            else:
                order_result = await self._executor.replace(intent)
            return order_result, True, None
        except Exception as exc:
            error = str(exc)
            return (
                _synthetic_order_result(intent, status=OrderResultStatus.FAILED, reason=error, retryable=True),
                True,
                error,
            )


@dataclass(frozen=True, slots=True)
class TradingReviewResult:
    intent: ManagedOrderIntent
    operation: str
    risk_decision: RiskDecision | None
    submitted: bool
    order_result: OrderResult | None = None
    submission_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.risk_decision.passed if self.risk_decision is not None else self.submitted


def _synthetic_order_result(
    intent: ManagedOrderIntent,
    *,
    status: OrderResultStatus,
    reason: str,
    retryable: bool,
) -> OrderResult:
    if isinstance(intent, (BuyOrderIntent, SellOrderIntent)):
        side = intent.side
        order_type = intent.order_type
        price = intent.price
        amount_usdc = intent.amount_usdc
        size_shares = intent.size_shares
        notional_usdc = intent.notional_usdc
    else:
        side = None
        order_type = None
        price = None
        amount_usdc = None
        size_shares = None
        notional_usdc = Decimal("0")
    return OrderResult(
        strategy_id=intent.strategy_id,
        trace_id=intent.trace_id,
        condition_id=intent.condition_id,
        token_id=intent.token_id,
        status=status,
        intent=intent,
        market_slug=intent.market_slug,
        side=side,
        order_type=order_type,
        price=price,
        requested_amount_usdc=amount_usdc,
        requested_size_shares=size_shares,
        notional_usdc=notional_usdc,
        reason=reason,
        retryable=retryable,
    )
