from __future__ import annotations

from decimal import Decimal

from polymarket_trader.app.order_projection import (
    AccountStateProjector,
    find_open_order,
    order_open_shares,
    order_open_size,
    order_result_to_order_status,
)
from polymarket_trader.app.reconcile_service import ReconcileAction, ReconcileActionType
from polymarket_trader.app.order_gateway import OrderGateway
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    OrderRecord,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.serialization import utc_now


class ReconcileActionApplier:
    def __init__(
        self,
        *,
        order_gateway: OrderGateway | None,
        account_state_store: AccountStateStore | None,
    ) -> None:
        self._trading_service = order_gateway
        self._account_state_store = account_state_store

    async def apply(
        self,
        action: ReconcileAction,
        market: Market,
        account_snapshot: AccountSnapshot,
    ) -> None:
        if action.action_type == ReconcileActionType.PAUSE_TRADING:
            return
        if action.action_type == ReconcileActionType.RESUME_TRADING:
            await self._apply_resume(action)
            return
        if action.action_type == ReconcileActionType.CANCEL_ORDER:
            await self._apply_cancel(action, account_snapshot)
            return
        if action.action_type == ReconcileActionType.REPLACE_ORDER:
            await self._apply_replace(action, account_snapshot)
            return
        if action.action_type == ReconcileActionType.SUBMIT_ORDER:
            await self._apply_submit_order(action, market, account_snapshot)
            return
        raise RuntimeError(f"unsupported reconcile action: {action.action_type}")

    async def _apply_resume(self, action: ReconcileAction) -> None:
        if self._account_state_store is not None:
            self._account_state_store.resume_market(action.condition_id)

    async def _apply_cancel(self, action: ReconcileAction, account_snapshot: AccountSnapshot) -> None:
        cancel_intent = action.intent
        if not isinstance(cancel_intent, CancelOrderIntent):
            raise TypeError("cancel action is missing cancel intent")
        if self._trading_service is None:
            raise RuntimeError("trading_service_required")

        review = await self._trading_service.cancel(cancel_intent)
        result = review.order_result
        cancelled = (
            review.submitted
            and isinstance(result, OrderResult)
            and result.status == OrderResultStatus.CANCELLED
        )
        if not cancelled:
            return

        if self._account_state_store is not None and action.source_order_id is not None:
            token_id = _require_token_id(action)
            self._account_state_store.remove_order(action.source_order_id)
            position = account_snapshot.get_position(action.condition_id, token_id)
            if position is not None:
                if action.source_order_side == OrderSide.BUY:
                    updated_position = position.with_open_buy_shares(Decimal("0"))
                    updated_position = updated_position.with_pending_buy_shares(Decimal("0"))
                else:
                    remaining = max(
                        position.open_sell_shares - (action.target_size_shares or Decimal("0")),
                        Decimal("0"),
                    )
                    updated_position = position.with_open_sell_shares(remaining)
                self._account_state_store.upsert_position(updated_position)

    async def _apply_submit_order(
        self,
        action: ReconcileAction,
        market: Market,
        account_snapshot: AccountSnapshot,
    ) -> None:
        trade_intent = action.intent
        if not isinstance(trade_intent, (BuyOrderIntent, SellOrderIntent)):
            raise TypeError("submit action is missing trade intent")
        if self._trading_service is None:
            raise RuntimeError("trading_service_required")
        token_id = _require_token_id(action)

        review = await self._trading_service.review_intent(
            trade_intent,
            market=market,
            position=account_snapshot.get_position(action.condition_id, token_id),
            open_orders=account_snapshot.open_orders_for_market(action.condition_id, token_id),
            classification_passed=True,
            market_active=market.trading_status == TradingStatus.ELIGIBLE,
            market_open=market.trading_status == TradingStatus.ELIGIBLE,
            clob_enabled=True,
            resolved=market.trading_status == TradingStatus.RESOLVED,
            cancelled=False,
            archived=market.trading_status == TradingStatus.CLOSED,
            balance_usdc=account_snapshot.balance_usdc,
            allowance_usdc=account_snapshot.allowance_usdc,
            min_order_size=market.min_order_size,
            operation=trade_intent.side.value.lower(),
        )
        submitted = review.submitted and _submission_succeeded(review.order_result)

        if self._account_state_store is not None and submitted:
            AccountStateProjector(
                self._account_state_store,
            ).apply_submitted_intent(
                trade_intent,
                market_slug=action.market_slug,
                snapshot=account_snapshot,
                reason="reconcile_submit_order",
            )

    async def _apply_replace(
        self,
        action: ReconcileAction,
        account_snapshot: AccountSnapshot,
    ) -> None:
        replace_intent = action.intent
        if not isinstance(replace_intent, ReplaceOrderIntent):
            raise TypeError("replace action is missing replace intent")
        if self._trading_service is None:
            raise RuntimeError("trading_service_required")

        review = await self._trading_service.replace(replace_intent)
        result = review.order_result
        submitted = review.submitted and _submission_succeeded(result)

        if self._account_state_store is None or not submitted:
            return

        current_snapshot = self._account_state_store.snapshot()
        token_id = _require_token_id(action)
        existing_order = find_open_order(
            current_snapshot,
            action.condition_id,
            token_id,
            action.source_order_id,
        ) or find_open_order(
            account_snapshot,
            action.condition_id,
            token_id,
            action.source_order_id,
        )
        existing_size = Decimal("0") if existing_order is None else order_open_size(existing_order)
        existing_shares = (
            Decimal("0")
            if existing_order is None
            else (order_open_shares(existing_order) or Decimal("0"))
        )
        if action.source_order_id is not None:
            self._account_state_store.remove_order(action.source_order_id)

        replacement_order_id = replace_intent.order_id
        replacement_remaining = replace_intent.size_shares
        replacement_price = replace_intent.new_price
        replacement_status = OrderStatus.SUBMITTED
        matched_shares = Decimal("0")
        trade_id = None
        replacement_side = OrderSide.SELL if existing_order is None else existing_order.side
        replacement_order_type = OrderType.GTC if existing_order is None else existing_order.order_type
        if isinstance(result, OrderResult):
            replacement_order_id = result.order_id or replacement_order_id
            replacement_price = result.price or replacement_price
            matched_shares = result.matched_shares
            trade_id = result.trade_id
            replacement_status = order_result_to_order_status(result)
            replacement_side = result.side or replacement_side
            replacement_order_type = result.order_type or replacement_order_type
            replacement_remaining = result.remaining_shares
            if (
                result.status in {OrderResultStatus.LIVE, OrderResultStatus.PARTIAL_FILL}
                and replacement_remaining <= Decimal("0")
            ):
                replacement_remaining = max(
                    replace_intent.size_shares - result.matched_shares,
                    Decimal("0"),
                )
        if replacement_remaining < Decimal("0"):
            replacement_remaining = Decimal("0")

        if replacement_status in {
            OrderStatus.SUBMITTED,
            OrderStatus.LIVE,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CREATED,
            OrderStatus.SIGNED,
        }:
            self._account_state_store.upsert_order(
                OrderRecord(
                    trace_id=replace_intent.trace_id,
                    condition_id=replace_intent.condition_id,
                    token_id=replace_intent.token_id,
                    side=replacement_side,
                    order_type=replacement_order_type,
                    price=replacement_price,
                    market_slug=replace_intent.market_slug,
                    size_shares=replace_intent.size_shares,
                    filled_shares=matched_shares,
                    remaining_shares=replacement_remaining,
                    notional_usdc=replacement_price * replace_intent.size_shares,
                    order_id=replacement_order_id,
                    trade_id=trade_id,
                    status=replacement_status,
                    idempotency_key=replace_intent.idempotency_key or replacement_order_id,
                    reason=replace_intent.reason or "reconcile_replace_order",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
        else:
            self._account_state_store.remove_order(replacement_order_id)

        current_position = current_snapshot.get_position(action.condition_id, token_id)
        if current_position is None:
            current_position = account_snapshot.get_position(action.condition_id, token_id)
        if current_position is None:
            return
        if replacement_side == OrderSide.SELL:
            updated_open_sell = (
                max(current_position.open_sell_shares - existing_size, Decimal("0"))
                + replacement_remaining
            )
            self._account_state_store.upsert_position(
                current_position.with_open_sell_shares(updated_open_sell)
            )
        else:
            updated_open_buy = (
                max(current_position.open_buy_shares - existing_shares, Decimal("0"))
                + replacement_remaining
            )
            updated_position = current_position.with_open_buy_shares(updated_open_buy)
            self._account_state_store.upsert_position(
                updated_position.with_pending_buy_shares(updated_open_buy)
            )


def _submission_succeeded(result: OrderResult | None) -> bool:
    if result is None:
        return True
    return result.status not in {
        OrderResultStatus.REJECTED,
        OrderResultStatus.FAILED,
        OrderResultStatus.UNKNOWN_TIMEOUT,
    }


def _require_token_id(action: ReconcileAction) -> str:
    if action.token_id is None:
        raise RuntimeError("reconcile_action_token_id_required")
    return action.token_id
