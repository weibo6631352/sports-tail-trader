from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents

from polymarket_trader.app.admin_operations import market_status_allowed_for_manual_order
from polymarket_trader.app.admin_serialization import AdminSerializer, decimal_text
from polymarket_trader.app.order_projection import AccountStateProjector, normalize_order_id, order_open_shares
from polymarket_trader.pipeline.execution.order_gateway import OrderGateway
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order, OrderResultStatus, ReplaceOrderIntent


class MarketResolver(Protocol):
    def __call__(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Market | None: ...


class OpenOrderFinder(Protocol):
    def __call__(
        self,
        snapshot: AccountSnapshot,
        *,
        order_id: str,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Order | None: ...


class AdminOrderController:
    def __init__(
        self,
        *,
        runtime: RuntimeComponents | None,
        serializer: AdminSerializer,
        account_snapshot: Callable[[], AccountSnapshot],
        resolve_market: MarketResolver,
        order_gateway: Callable[[], OrderGateway],
        find_open_order: OpenOrderFinder,
    ) -> None:
        self._runtime = runtime
        self._serializer = serializer
        self._account_snapshot = account_snapshot
        self._resolve_market = resolve_market
        self._order_gateway = order_gateway
        self._find_open_order = find_open_order

    async def replace_order(
        self,
        *,
        order_id: str,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        new_price: Decimal,
        size_shares: Decimal | None = None,
        operator: str = "manual",
        reason: str = "admin_replace_order",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = trace_id or uuid4().hex
        account = self._account_snapshot()
        source_order = self._find_open_order(
            account,
            order_id=order_id,
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
        )
        if source_order is None:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "order_not_found",
                "order_id": order_id,
            }

        market = self._resolve_market(
            market_slug=market_slug or source_order.market_slug,
            condition_id=condition_id or source_order.condition_id,
            token_id=token_id or source_order.token_id,
        )
        failure = self._validate_replace_request(
            trace_id=trace_id,
            market=market,
            source_order=source_order,
            new_price=new_price,
        )
        if failure is not None:
            return failure
        if market is None:
            raise RuntimeError(f"replace_order: market not resolved for order_id={order_id}")

        requested_size_shares = size_shares or order_open_shares(source_order)
        if requested_size_shares is None or requested_size_shares <= Decimal("0"):
            return self._failed_with_order(
                trace_id=trace_id,
                reason="order_size_unknown",
                market=market,
                order=source_order,
            )

        try:
            order_gateway = self._order_gateway()
        except RuntimeError as exc:
            return self._failed_with_order(
                trace_id=trace_id,
                reason=str(exc),
                market=market,
                order=source_order,
            )

        replace_intent = ReplaceOrderIntent(
            trace_id=trace_id,
            condition_id=source_order.condition_id,
            token_id=source_order.token_id,
            order_id=normalize_order_id(source_order),
            new_price=new_price,
            size_shares=requested_size_shares,
            market_slug=source_order.market_slug or market.market_slug,
            side=source_order.side,  # 从原 order 透传 side 给 paper simulate_fill / 实盘 replace
            reason=reason,
        )
        replace_review = await order_gateway.replace(replace_intent)
        replace_result = replace_review.order_result
        await self._publish_replace_audit(
            trace_id=trace_id,
            order=source_order,
            new_price=new_price,
            new_size_shares=requested_size_shares,
            operator=operator,
            reason=reason,
            order_result=replace_result,
        )
        if replace_result is None or replace_result.status in {
            OrderResultStatus.FAILED,
            OrderResultStatus.REJECTED,
        }:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "replace_order_failed",
                "market": self._serializer.market(market),
                "order": self._serializer.order(source_order),
                "replace_review": self._serializer.review(replace_review),
            }

        account_state = self._runtime.account_state_store if self._runtime is not None else None
        if account_state is not None:
            AccountStateProjector(account_state).apply_replace_result(
                market,
                source_order=source_order,
                result=replace_result,
                operator=operator,
                reason=reason,
            )
        return {
            "status": "ok",
            "trace_id": trace_id,
            "operator": operator,
            "market": self._serializer.market(market),
            "order": self._serializer.order(source_order),
            "replace_review": self._serializer.review(replace_review),
            "replace_order_submitted": self._serializer.order_result(replace_result),
        }

    async def _publish_replace_audit(
        self,
        *,
        trace_id: str,
        order: Order,
        new_price: Decimal,
        new_size_shares: Decimal,
        operator: str,
        reason: str,
        order_result: Any,
    ) -> None:
        """Admin 触发 replace 必须落 audit。worker 路径的 ORDER_CANCEL_REQUESTED 不覆盖
        管理面 replace；CLAUDE.md §10「可审计」要求 admin 写动作 + 结果都进 outbox。"""

        event_bus = self._runtime.event_bus if self._runtime is not None else None
        if event_bus is None:
            return
        payload = {
            "operator": operator,
            "reason": reason,
            "order_id": order.order_id or normalize_order_id(order),
            "old_price": str(order.price),
            "new_price": str(new_price),
            "old_size_shares": (
                str(order_open_shares(order)) if order_open_shares(order) is not None else None
            ),
            "new_size_shares": str(new_size_shares),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        if order_result is not None:
            payload["result_status"] = (
                order_result.status.value if order_result.status is not None else ""
            )
        await event_bus.publish(
            OutboxPriority.P1,
            DomainEvent(
                trace_id=trace_id,
                event_type=DomainEventType.REPLACE_ORDER_SUBMITTED,
                event_id=uuid4().hex,
                market_slug=order.market_slug,
                condition_id=order.condition_id,
                token_id=order.token_id,
                reason=reason,
                payload=payload,
            ),
        )

    def _validate_replace_request(
        self,
        *,
        trace_id: str,
        market: Market | None,
        source_order: Order,
        new_price: Decimal,
    ) -> dict[str, Any] | None:
        if market is None:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "market_not_found",
                "order": self._serializer.order(source_order),
            }
        if not market_status_allowed_for_manual_order(market):
            return self._failed_with_order(
                trace_id=trace_id,
                reason="market_not_operable",
                market=market,
                order=source_order,
            )
        if new_price <= Decimal("0") or new_price >= Decimal("1"):
            return self._failed_with_order(
                trace_id=trace_id,
                reason="invalid_price",
                market=market,
                order=source_order,
            )
        if market.tick_size <= Decimal("0"):
            return self._failed_with_order(
                trace_id=trace_id,
                reason="invalid_tick_size",
                market=market,
                order=source_order,
            )
        tick_remainder = (new_price % market.tick_size) if market.tick_size else Decimal("0")
        if tick_remainder != Decimal("0"):
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "price_not_aligned_to_tick_size",
                "market": self._serializer.market(market),
                "order": self._serializer.order(source_order),
                "new_price": decimal_text(new_price),
            }
        return None

    def _failed_with_order(
        self,
        *,
        trace_id: str,
        reason: str,
        market: Market | None,
        order: Order,
    ) -> dict[str, Any]:
        payload = {
            "status": "failed",
            "trace_id": trace_id,
            "reason": reason,
            "order": self._serializer.order(order),
        }
        if market is not None:
            payload["market"] = self._serializer.market(market)
        return payload
