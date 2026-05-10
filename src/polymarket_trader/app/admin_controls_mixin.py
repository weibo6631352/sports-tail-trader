"""AdminService 受控操作 mixin。

包含人工触发的可写动作：reconcile、replace_order、upsert_sports_live_state、
confirm_sports_tail_candidate。这些动作仍走 TradingService -> RiskManager ->
OrderExecutor 的主链路，不绕过统一服务。

宿主 AdminService 提供 ``runtime``、``_order_controller``、``_entry_metadata_store``、
``_serializer``、``_resolve_market``、``_market_ws_snapshot``、``_account_snapshot``、
``_build_entry_plan_for_admin``、``_trading_service``、``_trading_decision_service``、
``_project_manual_entry_result``、``_publish_candidate_confirmation_review``、
``_settings_value`` 等私有 helper。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import uuid4

from polymarket_trader.app.admin_operations import normalize_condition_ids
from polymarket_trader.domain.order import OrderResultStatus
from polymarket_trader.workers.trading_decision import (
    snapshot_allowance,
    snapshot_available_usdc,
)


class AdminControlsMixin:
    """受控人工操作。每个方法仍经过统一交易主链路。"""

    runtime: Any | None  # 宿主声明真正的字段；这里只是给类型检查器看

    async def reconcile(
        self,
        *,
        trace_id: str | None = None,
        condition_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        reconcile_worker = getattr(self.runtime, "reconcile_worker", None)
        if reconcile_worker is None:
            return {
                "status": "failed",
                "reason": "reconcile_worker_unavailable",
                "trace_id": trace_id or uuid4().hex,
            }
        condition_id_filter = normalize_condition_ids(condition_ids)
        result = await reconcile_worker.reconcile_once(
            trace_id=trace_id,
            condition_ids=condition_id_filter or None,
        )
        return self._serializer().reconcile_result(result)

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
        return await self._order_controller().replace_order(
            order_id=order_id,
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
            new_price=new_price,
            size_shares=size_shares,
            operator=operator,
            reason=reason,
            trace_id=trace_id,
        )

    async def upsert_sports_live_state(
        self,
        *,
        sports_tail_game: Mapping[str, Any],
        condition_id: str | None = None,
        market_slug: str | None = None,
        event_slug: str | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        """写入体育直播状态 metadata，不触发交易判断。"""

        store = self._entry_metadata_store()
        if store is None:
            return {"status": "failed", "reason": "entry_metadata_store_unavailable"}
        record = store.upsert(
            condition_id=condition_id,
            market_slug=market_slug,
            event_slug=event_slug,
            source=source,
            metadata={"sports_tail_game": dict(sports_tail_game)},
        )
        return {"status": "ok", "record": record.as_payload()}


    async def confirm_sports_tail_candidate(
        self,
        *,
        condition_id: str | None = None,
        token_id: str,
        market_slug: str | None = None,
        operator: str = "manual",
        note: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """人工确认体育扫尾候选，并经交易服务和风控提交。"""

        trace_id = trace_id or uuid4().hex
        market = self._resolve_market(
            condition_id=condition_id,
            market_slug=market_slug,
            token_id=token_id,
        )
        if market is None:
            return {"status": "failed", "trace_id": trace_id, "reason": "market_not_found"}
        if token_id not in market.token_ids:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "token_not_found",
                "market": self._serializer().market(market),
            }
        orderbook = self._market_ws_snapshot(token_id)
        if orderbook is None:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "orderbook_unavailable",
                "market": self._serializer().market(market),
            }

        account = self._account_snapshot()
        metadata = self._entry_metadata_for_market(market)
        candidate_plan = self._build_entry_plan_for_admin(
            market=market,
            token_id=token_id,
            orderbook=orderbook,
            account=account,
            trace_id=trace_id,
            metadata=metadata,
        )
        candidate = self._candidate_payload(market, token_id, candidate_plan)
        if not candidate["confirmable"]:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "candidate_not_confirmable",
                "candidate": candidate,
            }

        metadata.update(
            {
                "sports_tail_manual_confirmed": True,
                "sports_tail_confirmed_by": operator,
                "sports_tail_confirm_reason": note or "manual_confirm",
            }
        )
        plan = self._build_entry_plan_for_admin(
            market=market,
            token_id=token_id,
            orderbook=orderbook,
            account=account,
            trace_id=trace_id,
            metadata=metadata,
        )
        if not plan.ready_to_trade or plan.intent is None:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": plan.reason or "entry_plan_not_ready",
                "candidate": self._candidate_payload(market, token_id, plan),
            }

        review = await self._trading_service().review_intent(
            plan.intent,
            market=market,
            orderbook=orderbook,
            position=account.get_position(market.condition_id, token_id),
            open_orders=account.open_orders_for_market(market.condition_id, token_id),
            allocation_plan=plan.allocation_plan,
            classification_passed=True,
            balance_usdc=snapshot_available_usdc(account),
            allowance_usdc=snapshot_allowance(account),
            max_order_usdc=self._settings_value("max_order_usdc"),
            max_market_usdc=self._settings_value("max_market_usdc"),
            max_total_usdc=self._settings_value("max_total_usdc"),
            max_open_orders=self._settings_value("max_open_orders"),
            order_retry_limit=self._settings_value("order_retry_limit"),
            operation="admin_confirm_entry",
        )
        self._project_manual_entry_result(review, snapshot=account)
        await self._publish_candidate_confirmation_review(market=market, plan=plan, review=review)
        order_result = review.order_result
        failed = (
            order_result is None
            or order_result.status in {OrderResultStatus.FAILED, OrderResultStatus.REJECTED}
        )
        return {
            "status": "failed" if failed else "ok",
            "trace_id": trace_id,
            "reason": (
                review.risk_decision.reason
                if review.risk_decision is not None and not review.risk_decision.passed
                else (order_result.reason if order_result is not None else "")
            ),
            "candidate": self._candidate_payload(market, token_id, plan),
            "review": self._serializer().review(review),
        }


