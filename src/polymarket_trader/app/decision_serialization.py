"""交易决策相关的 audit payload 序列化 / 账户快照辅助函数。

这些函数既被 trading_decision_worker 的事件发布路径使用，也被 admin_service
的候选展示和人工确认路径使用；统一放在 app 层避免 app → workers 反向依赖。
Workers 通过 ``workers.trading_decision.event_payloads`` 的 re-export 保持原有调用界面。
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    ManagedOrderIntent,
    OrderResult,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.domain.position import Position
from polymarket_trader.serialization import jsonable

if TYPE_CHECKING:
    from polymarket_trader.app.decision_context_builder import EntryPlan
    from polymarket_trader.app.order_gateway import TradingReviewResult

TRADING_DECISION_WORKER_ORIGIN = "trading_decision_worker"


# ---------------------------------------------------------------------------
# Account snapshot helpers
# ---------------------------------------------------------------------------


def snapshot_balance(snapshot: AccountSnapshot | None) -> Decimal:
    if snapshot is None:
        return Decimal("0")
    return snapshot.balance_usdc


def snapshot_available_usdc(snapshot: AccountSnapshot | None) -> Decimal:
    if snapshot is None:
        return Decimal("0")
    return snapshot.available_usdc


def snapshot_allowance(snapshot: AccountSnapshot | None) -> Decimal:
    if snapshot is None:
        return Decimal("0")
    return snapshot.allowance_usdc


def snapshot_position(
    snapshot: AccountSnapshot | None,
    condition_id: str,
    token_id: str,
) -> Position | None:
    if snapshot is None:
        return None
    return snapshot.get_position(condition_id, token_id)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_snapshot(snapshot: AccountSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "balance_usdc": str(snapshot.balance_usdc),
        "allowance_usdc": str(snapshot.allowance_usdc),
        "user_ws_connected": snapshot.user_ws_connected,
        "allow_new_entries": snapshot.allow_new_entries,
        "market_pauses": [pause.as_payload() for pause in snapshot.market_pauses],
        "last_reconcile_at": (
            None if snapshot.last_reconcile_at is None else snapshot.last_reconcile_at.isoformat()
        ),
        "positions": [
            {
                "condition_id": position.condition_id,
                "token_id": position.token_id,
                "shares": str(position.shares),
                "cost_usdc": str(position.cost_usdc),
                "open_buy_shares": str(position.open_buy_shares),
                "open_sell_shares": str(position.open_sell_shares),
                "pending_buy_shares": str(position.pending_buy_shares),
            }
            for position in snapshot.positions
        ],
        "open_orders": [
            {
                "condition_id": order.condition_id,
                "token_id": order.token_id,
                "side": order.side.value,
                "status": order.status.value,
                "order_id": order.order_id,
                "idempotency_key": order.idempotency_key,
                "remaining_shares": (
                    None if order.remaining_shares is None else str(order.remaining_shares)
                ),
            }
            for order in snapshot.open_orders
        ],
    }


def serialize_allocation_plan(plan: EntryPlan) -> dict[str, object]:
    return {
        "trace_id": plan.allocation_plan.trace_id,
        "total_budget_usdc": str(plan.allocation_plan.total_budget_usdc),
        "allocated_budget_usdc": str(plan.allocation_plan.allocated_budget_usdc),
        "released_budget_usdc": str(plan.allocation_plan.released_budget_usdc),
        "eligible_market_count": plan.eligible_market_count,
        "reason": plan.allocation_plan.reason,
    }


def serialize_allocation(plan: EntryPlan) -> dict[str, object] | None:
    if plan.allocation is None:
        return None
    a = plan.allocation
    payload: dict[str, object] = {
        "condition_id": a.condition_id,
        "target_budget_usdc": str(a.target_budget_usdc),
        "buy_budget_usdc": str(a.buy_budget_usdc),
        "current_exposure_usdc": str(a.current_exposure_usdc),
        "released_budget_usdc": str(a.released_budget_usdc),
        "reason": a.reason,
        "release_reason": a.release_reason,
    }
    for key in (
        "prob_p", "prob_confidence", "price_c",
        "edge_net", "edge_gross", "fee_per_share_usdc",
        "kelly_f_star", "effective_kelly_fraction", "effective_min_stake_usdc",
    ):
        value = getattr(a, key)
        if value is not None:
            payload[key] = str(value)
    if a.capped_by is not None:
        payload["capped_by"] = a.capped_by
    if a.is_round_up_overbet:
        payload["is_round_up_overbet"] = True
    return payload


def serialize_plan_metadata(plan: EntryPlan) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision_kind": None if plan.decision_kind is None else plan.decision_kind.value,
        "intent_tags": tuple(sorted(plan.intent.intent_tags)) if plan.intent is not None else (),
        "strategy_payload": jsonable(plan.metadata or {}),
    }
    if plan.summary is not None:
        summary = plan.summary
        payload["strategy_summary"] = {
            "action": summary.action,
            "reason": summary.reason,
            "label": summary.label,
            "market_type": summary.market_type,
            "side": summary.side,
            "line": None if summary.line is None else str(summary.line),
            "best_ask": None if summary.best_ask is None else str(summary.best_ask),
            "observed_at": None if summary.observed_at is None else summary.observed_at.isoformat(),
            "manual_confirmed": summary.manual_confirmed,
            "confirmed_by": summary.confirmed_by,
            "confirm_reason": summary.confirm_reason,
            "extras": jsonable(summary.extras or {}),
        }
    else:
        payload["strategy_summary"] = None
    return payload


def serialize_intent(intent: ManagedOrderIntent) -> dict[str, object]:
    payload: dict[str, object] = {
        "trace_id": intent.trace_id,
        "condition_id": intent.condition_id,
        "token_id": intent.token_id,
        "market_slug": intent.market_slug,
        "side": None,
        "order_type": None,
        "price": None,
        "amount_usdc": None,
        "size_shares": None,
        "order_id": None,
        "reason": "",
        "allow_open_exit_overlap": False,
        "metadata": {},
    }
    if isinstance(intent, (BuyOrderIntent, SellOrderIntent)):
        payload["side"] = intent.side.value
        payload["order_type"] = intent.order_type.value
        payload["price"] = str(intent.price) if intent.price is not None else None
        amount_usdc = intent.amount_usdc
        size_shares = intent.size_shares
        payload["amount_usdc"] = None if amount_usdc is None else str(amount_usdc)
        payload["size_shares"] = None if size_shares is None else str(size_shares)
        payload["metadata"] = jsonable(intent.metadata or {})
        if isinstance(intent, BuyOrderIntent):
            payload["allow_open_exit_overlap"] = bool(intent.allow_open_exit_overlap)
    elif isinstance(intent, (CancelOrderIntent, ReplaceOrderIntent)):
        payload["order_id"] = intent.order_id
        payload["reason"] = intent.reason
        if isinstance(intent, ReplaceOrderIntent):
            payload["price"] = str(intent.new_price)
            payload["size_shares"] = str(intent.size_shares)
    return payload


def serialize_review(review: TradingReviewResult) -> dict[str, object]:
    return {
        "operation": review.operation,
        "submitted": review.submitted,
        "submission_error": review.submission_error,
        "risk_decision": None
        if review.risk_decision is None
        else {
            "passed": review.risk_decision.passed,
            "reason": review.risk_decision.reason,
            "failed_field": review.risk_decision.failed_field,
            "suggested_action": review.risk_decision.suggested_action,
            "retryable": review.risk_decision.retryable,
        },
        "order_result": serialize_order_result(review.order_result)
        if review.order_result is not None
        else None,
    }


def serialize_control_intent(intent: CancelOrderIntent) -> dict[str, object]:
    return {
        "trace_id": intent.trace_id,
        "condition_id": intent.condition_id,
        "token_id": intent.token_id,
        "order_id": intent.order_id,
        "market_slug": intent.market_slug,
        "reason": intent.reason,
    }


def serialize_order_result(order_result: OrderResult | None) -> dict[str, object] | None:
    if order_result is None:
        return None
    return {
        "trace_id": order_result.trace_id,
        "condition_id": order_result.condition_id,
        "token_id": order_result.token_id,
        "status": order_result.status.value,
        "market_slug": order_result.market_slug,
        "order_id": order_result.order_id,
        "trade_id": order_result.trade_id,
        "side": None if order_result.side is None else order_result.side.value,
        "order_type": None if order_result.order_type is None else order_result.order_type.value,
        "price": None if order_result.price is None else str(order_result.price),
        "requested_amount_usdc": None
        if order_result.requested_amount_usdc is None
        else str(order_result.requested_amount_usdc),
        "requested_size_shares": None
        if order_result.requested_size_shares is None
        else str(order_result.requested_size_shares),
        "matched_shares": str(order_result.matched_shares),
        "remaining_shares": str(order_result.remaining_shares),
        "spent_usdc": str(order_result.spent_usdc),
        "notional_usdc": str(order_result.notional_usdc),
        "reason": order_result.reason,
        "retryable": order_result.retryable,
        "raw_response_summary": order_result.raw_response_summary,
    }
