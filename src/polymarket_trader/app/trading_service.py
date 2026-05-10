from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from inspect import isawaitable
from typing import Any, Iterable

from polymarket_trader.domain.allocation import AllocationPlan
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    ExecutionTimestamps,
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
from polymarket_trader.extension_api.lifecycle import LifecycleEvent
from polymarket_trader.runtime.lifecycle_bus import LifecyclePublisher


class TradingService:
    """Coordinates risk-checked order intents and execution."""

    def __init__(
        self,
        *,
        risk_manager: RiskManager | None = None,
        executor: object | None = None,
        lifecycle_bus: LifecyclePublisher | None = None,
    ) -> None:
        self._risk_manager = risk_manager or RiskManager()
        self._executor = executor
        self._lifecycle_bus = lifecycle_bus

    async def review_intent(
        self,
        intent: OrderIntent,
        *,
        market: Market | None = None,
        orderbook: OrderbookSnapshot | None = None,
        position: Position | None = None,
        open_orders: Iterable[Order] = (),
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
        retry_count: int = 0,
        order_retry_limit: int | None = None,
        max_order_usdc: Decimal | None = None,
        max_market_usdc: Decimal | None = None,
        max_total_usdc: Decimal | None = None,
        max_open_orders: int | None = None,
        min_order_size: Decimal | None = None,
        operation: str = "review",
    ) -> "TradingReviewResult":
        risk_decision = self._risk_manager.check_order_intent(
            intent,
            market=market,
            orderbook=orderbook,
            position=position,
            open_orders=open_orders,
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
            retry_count=retry_count,
            order_retry_limit=order_retry_limit,
            max_order_usdc=max_order_usdc,
            max_market_usdc=max_market_usdc,
            max_total_usdc=max_total_usdc,
            max_open_orders=max_open_orders,
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
        intent_tags = getattr(intent, "intent_tags", None)
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
                _synthetic_order_result(
                    intent,
                    status=OrderResultStatus.FAILED,
                    reason="executor_unavailable",
                    retryable=True,
                ),
                False,
                None,
            )

        result, submitted, submission_error = await self._invoke_executor(
            intent,
            operation=operation,
            method_candidates=("submit", f"submit_{intent.side.value.lower()}"),
            fallback_status=OrderResultStatus.FAILED,
            fallback_reason="executor_returned_empty_result",
        )
        return result, submitted, submission_error

    async def _execute_control_intent(
        self,
        intent: CancelOrderIntent | ReplaceOrderIntent,
        *,
        operation: str,
    ) -> tuple[OrderResult, bool, str | None]:
        if self._executor is None:
            return (
                _synthetic_order_result(
                    intent,
                    status=OrderResultStatus.FAILED,
                    reason="executor_unavailable",
                    retryable=True,
                ),
                False,
                None,
            )

        method_candidates = ("cancel", "cancel_order") if operation == "cancel" else ("replace", "replace_order")
        result, submitted, submission_error = await self._invoke_executor(
            intent,
            operation=operation,
            method_candidates=method_candidates,
            fallback_status=OrderResultStatus.FAILED,
            fallback_reason="executor_returned_empty_result",
        )
        return result, submitted, submission_error

    async def _invoke_executor(
        self,
        intent: ManagedOrderIntent,
        *,
        operation: str,
        method_candidates: tuple[str, ...],
        fallback_status: OrderResultStatus,
        fallback_reason: str,
    ) -> tuple[OrderResult, bool, str | None]:
        last_error: str | None = None
        for method_name in method_candidates:
            method = getattr(self._executor, method_name, None)
            if method is None:
                continue
            try:
                raw_result = method(intent)
                if isawaitable(raw_result):
                    raw_result = await raw_result
                order_result = _coerce_order_result(
                    raw_result,
                    intent,
                    operation=operation,
                    fallback_status=fallback_status,
                    fallback_reason=fallback_reason,
                )
                return order_result, True, None
            except Exception as exc:  # pragma: no cover - injected in tests
                last_error = str(exc)
                break
        if last_error is not None:
            return (
                _synthetic_order_result(
                    intent,
                    status=OrderResultStatus.FAILED,
                    reason=last_error,
                    retryable=True,
                ),
                True,
                last_error,
            )
        return (
            _synthetic_order_result(
                intent,
                status=fallback_status,
                reason=fallback_reason,
                retryable=False,
            ),
            False,
            None,
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


def _coerce_order_result(
    raw_result: object | None,
    intent: ManagedOrderIntent,
    *,
    operation: str,
    fallback_status: OrderResultStatus,
    fallback_reason: str,
) -> OrderResult:
    if isinstance(raw_result, OrderResult):
        return raw_result
    if raw_result is not None and hasattr(raw_result, "__dict__"):
        data = dict(vars(raw_result))
    elif isinstance(raw_result, dict):
        data = dict(raw_result)
    else:
        data = {}

    status_value = data.get("status")
    if isinstance(status_value, OrderResultStatus):
        status = status_value
    elif isinstance(status_value, str):
        try:
            status = OrderResultStatus(status_value)
        except ValueError:
            status = fallback_status
    else:
        status = fallback_status

    timestamps = data.get("timestamps")
    if not isinstance(timestamps, dict) and timestamps is not None:
        timestamps = {
            "queued_at": getattr(timestamps, "queued_at", None),
            "sign_started_at": getattr(timestamps, "sign_started_at", None),
            "signed_at": getattr(timestamps, "signed_at", None),
            "submitted_at": getattr(timestamps, "submitted_at", None),
            "ack_at": getattr(timestamps, "ack_at", None),
        }

    return OrderResult(
        trace_id=str(data.get("trace_id", intent.trace_id)),
        condition_id=str(data.get("condition_id", intent.condition_id)),
        token_id=str(data.get("token_id", intent.token_id)),
        status=status,
        intent=intent,
        market_slug=data.get("market_slug", intent.market_slug),
        order_id=data.get("order_id"),
        trade_id=data.get("trade_id"),
        side=_coerce_side(data.get("side", getattr(intent, "side", None))),
        order_type=_coerce_order_type(data.get("order_type", getattr(intent, "order_type", None))),
        price=_coerce_decimal(data.get("price", getattr(intent, "price", None))),
        requested_amount_usdc=_coerce_decimal(
            data.get("requested_amount_usdc", getattr(intent, "amount_usdc", None))
        ),
        requested_size_shares=_coerce_decimal(
            data.get("requested_size_shares", getattr(intent, "size_shares", None))
        ),
        matched_shares=_coerce_decimal_or_zero(data.get("matched_shares", Decimal("0"))),
        remaining_shares=_coerce_decimal_or_zero(data.get("remaining_shares", Decimal("0"))),
        spent_usdc=_coerce_decimal_or_zero(data.get("spent_usdc", Decimal("0"))),
        notional_usdc=_coerce_decimal_or_zero(
            data.get("notional_usdc", getattr(intent, "notional_usdc", Decimal("0")))
        ),
        reason=data.get("reason", fallback_reason),
        retryable=bool(data.get("retryable", False)),
        raw_response_summary=data.get("raw_response_summary"),
        timestamps=(
            timestamps
            if isinstance(timestamps, ExecutionTimestamps)
            else ExecutionTimestamps(**timestamps)
            if isinstance(timestamps, dict)
            else ExecutionTimestamps()
        ),
    )


def _synthetic_order_result(
    intent: ManagedOrderIntent,
    *,
    status: OrderResultStatus,
    reason: str,
    retryable: bool,
) -> OrderResult:
    return OrderResult(
        trace_id=intent.trace_id,
        condition_id=intent.condition_id,
        token_id=intent.token_id,
        status=status,
        intent=intent,
        market_slug=intent.market_slug,
        side=_coerce_side(getattr(intent, "side", None)),
        order_type=_coerce_order_type(getattr(intent, "order_type", None)),
        price=_coerce_decimal(getattr(intent, "price", None)),
        requested_amount_usdc=_coerce_decimal(getattr(intent, "amount_usdc", None)),
        requested_size_shares=_coerce_decimal(getattr(intent, "size_shares", None)),
        notional_usdc=_coerce_decimal_or_zero(getattr(intent, "notional_usdc", Decimal("0"))),
        reason=reason,
        retryable=retryable,
    )


def _coerce_decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _coerce_decimal_or_zero(value: object | None) -> Decimal:
    return _coerce_decimal(value) or Decimal("0")


def _coerce_side(value: object | None):
    if value is None:
        return None
    try:
        from polymarket_trader.domain.order import OrderSide

        if isinstance(value, OrderSide):
            return value
        return OrderSide(str(value))
    except Exception:
        return None


def _coerce_order_type(value: object | None):
    if value is None:
        return None
    try:
        from polymarket_trader.domain.order import OrderType

        if isinstance(value, OrderType):
            return value
        return OrderType(str(value))
    except Exception:
        return None
