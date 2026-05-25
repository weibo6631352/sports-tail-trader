"""AdminService 受控操作 mixin。

包含人工触发的可写动作：reconcile、replace_order、upsert_live_state、
confirm_candidate。这些动作仍走 TradingService -> RiskManager ->
OrderExecutor 的主链路，不绕过统一服务。

宿主 AdminService 提供 ``runtime``、``_order_controller``、``_entry_metadata_store``、
``_serializer``、``_resolve_market``、``_market_ws_snapshot``、``_account_snapshot``、
``_build_entry_plan_for_admin``、``_trading_service``、``_trading_decision_service``、
``_project_manual_entry_result``、``_publish_candidate_confirmation_review``、
``_settings_value`` 等私有 helper。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents

from polymarket_trader.app.admin_operations import (
    market_status_allowed_for_manual_order,
    normalize_condition_ids,
)
from polymarket_trader.app.order_projection import normalize_order_id, order_open_shares
from polymarket_trader.domain.account import AccountSnapshot, MarketPauseSource
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    Order,
    OrderResultStatus,
    OrderSide,
    OrderType,
    SellOrderIntent,
)
from polymarket_trader.domain.decisions import ManualConfirmation
from polymarket_trader.app.decision_serialization import (
    snapshot_allowance,
    snapshot_available_usdc,
)
from polymarket_trader.quant.config import CurrentStrategyConfig

logger = logging.getLogger(__name__)


def _resolve_admin_bankroll(account: AccountSnapshot, portfolio_budget_usdc: Any) -> Decimal:
    """同 EntryPlanner / worker 一致的 bankroll 口径。Admin/manual 入口同样要走 Kelly。"""

    cap = portfolio_budget_usdc if isinstance(portfolio_budget_usdc, Decimal) else Decimal(
        str(portfolio_budget_usdc) if portfolio_budget_usdc is not None else "0"
    )
    bankroll = min(account.available_usdc, cap)
    if bankroll < Decimal("0"):
        return Decimal("0")
    return bankroll


class AdminControlsMixin:
    """受控人工操作。每个方法仍经过统一交易主链路。"""

    runtime: RuntimeComponents | None  # 宿主声明真正的字段；这里只是给类型检查器看

    async def reconcile(
        self,
        *,
        trace_id: str | None = None,
        condition_ids: Sequence[str] | None = None,
        reason: str = "",
        authorized_by: str = "operator",
    ) -> dict[str, Any]:
        from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority

        reconcile_worker = self.runtime.reconcile_worker if self.runtime else None
        resolved_trace_id = trace_id or uuid4().hex
        if reconcile_worker is None:
            return {
                "status": "failed",
                "reason": "reconcile_worker_unavailable",
                "trace_id": resolved_trace_id,
            }
        condition_id_filter = normalize_condition_ids(condition_ids)
        logger.warning(
            "admin reconcile triggered",
            extra={
                "authorized_by": authorized_by,
                "reason": reason,
                "trace_id": resolved_trace_id,
                "condition_ids": list(condition_id_filter) if condition_id_filter else [],
            },
        )
        event_bus = self.runtime.event_bus if self.runtime else None
        if event_bus is not None:
            event_bus.publish_nowait(
                OutboxPriority.P1,
                DomainEvent(
                    trace_id=resolved_trace_id,
                    event_type=DomainEventType.RECONCILE_STARTED,
                    event_id=uuid4().hex,
                    reason=reason or "admin_reconcile",
                    payload={
                        "authorized_by": authorized_by,
                        "reason": reason,
                        "condition_ids": list(condition_id_filter) if condition_id_filter else [],
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                    },
                ),
            )
        result = await reconcile_worker.reconcile_once(
            trace_id=resolved_trace_id,
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

    async def upsert_live_state(
        self,
        *,
        payload: Mapping[str, Any],
        signal_allowed: bool | None = None,
        signal_reason: str = "",
        condition_id: str | None = None,
        market_slug: str | None = None,
        event_slug: str | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        """人工写入策略可见的 live_state 状态。framework 不解析 ``payload`` 字段语义。"""

        store = self._entry_metadata_store()
        if store is None:
            return {"status": "failed", "reason": "entry_metadata_store_unavailable"}
        record = store.upsert(
            condition_id=condition_id,
            market_slug=market_slug,
            event_slug=event_slug,
            source=source,
            metadata=dict(payload),
            live_state_signal_allowed=signal_allowed,
            live_state_signal_reason=signal_reason,
            live_state_payload=dict(payload),
        )
        return {"status": "ok", "record": record.as_payload()}


    async def confirm_candidate(
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

        confirmation = ManualConfirmation(
            operator=operator,
            reason=note or "manual_confirm",
            confirmed_at=datetime.now(timezone.utc),
        )
        plan = self._build_entry_plan_for_admin(
            market=market,
            token_id=token_id,
            orderbook=orderbook,
            account=account,
            trace_id=trace_id,
            metadata=metadata,
            manual_confirmation=confirmation,
        )
        if not plan.ready_to_trade or plan.intent is None:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": plan.reason or "entry_plan_not_ready",
                "candidate": self._candidate_payload(market, token_id, plan),
            }

        _kelly = self.runtime.strategy.config if self.runtime else CurrentStrategyConfig()
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
            bankroll_usdc=_resolve_admin_bankroll(account, self._settings_value("portfolio_budget_usdc")),
            kelly_max_position_fraction=_kelly.kelly_max_position_fraction,
            kelly_round_up_max_overbet_ratio=_kelly.kelly_round_up_max_overbet_ratio,
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

    async def record_market_settlement(
        self,
        *,
        condition_id: str,
        winning_token_id: str,
        winning_outcome: str | None = None,
        source: str = "manual",
        operator: str = "manual",
    ) -> dict[str, Any]:
        """手工记录市场结算结果——触发 ``MARKET_SETTLED`` 事件。

        当前没有自动 settlement 抓取链路；运维确认 outcome 后调用这里，事件
        会落 ``audit_events``（event_title=``market_settled``），给 calibration
        / Brier score 提供 ground truth。
        """

        from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority

        event_bus = self.runtime.event_bus if self.runtime else None
        if event_bus is None:
            return {"status": "failed", "reason": "event_bus_unavailable"}
        trace_id = uuid4().hex
        event_bus.publish_nowait(
            OutboxPriority.P3,
            DomainEvent(
                trace_id=trace_id,
                event_type=DomainEventType.MARKET_SETTLED,
                event_id=uuid4().hex,
                condition_id=condition_id,
                token_id=winning_token_id,
                reason="manual_settlement",
                payload={
                    "winning_token_id": winning_token_id,
                    "winning_outcome": winning_outcome,
                    "settled_at": datetime.now(timezone.utc).isoformat(),
                    "source": source,
                    "operator": operator,
                },
            ),
        )
        return {
            "status": "ok",
            "trace_id": trace_id,
            "condition_id": condition_id,
            "winning_token_id": winning_token_id,
            "winning_outcome": winning_outcome,
            "source": source,
            "operator": operator,
        }

    async def pause_trading(
        self,
        *,
        reason: str = "manual_pause",
        operator: str = "manual",
        authorized_by: str = "operator",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """人工触发暂停自动交易。phase 立即切到 PAUSED，等用户 resume 才恢复。

        trace_id 由前端 confirmAction 在 modal mount 时生成，让"操作意图 + 审计事件"串成一条链；
        缺失时本地生成兜底，避免老调用方破。"""

        from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority

        supervisor = self.runtime.supervisor if self.runtime else None
        if supervisor is None:
            return {"status": "failed", "reason": "supervisor_unavailable"}
        normalized_reason = reason.strip() or "manual_pause"
        phase_before = supervisor.snapshot().phase.value
        supervisor.pause_trading(normalized_reason)
        phase_after = supervisor.snapshot().phase.value

        # 主交易开关变更是高敏操作，必须落审计——CLAUDE.md §3 / §10
        # 要求拒绝、降级、恢复动作可审计；event_bus 不可用时不应静默失败。
        trace_id = trace_id or uuid4().hex
        logger.warning(
            "admin pause_trading triggered",
            extra={
                "operator": operator,
                "authorized_by": authorized_by,
                "reason": normalized_reason,
                "trace_id": trace_id,
                "phase_before": phase_before,
                "phase_after": phase_after,
            },
        )
        event_bus = self.runtime.event_bus if self.runtime else None
        if event_bus is not None:
            event_bus.publish_nowait(
                OutboxPriority.P1,
                DomainEvent(
                    trace_id=trace_id,
                    event_type=DomainEventType.TRADING_PAUSED,
                    event_id=uuid4().hex,
                    reason=normalized_reason,
                    payload={
                        "operator": operator,
                        "authorized_by": authorized_by,
                        "reason": normalized_reason,
                        "phase_before": phase_before,
                        "phase_after": phase_after,
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                    },
                ),
            )
        return {
            "status": "ok",
            "trace_id": trace_id,
            "operator": operator,
            "authorized_by": authorized_by,
            "reason": normalized_reason,
            "phase": phase_after,
            "phase_before": phase_before,
            "manual_pause_reason": normalized_reason,
            "audit_published": event_bus is not None,
        }

    async def resume_trading(
        self,
        *,
        operator: str = "manual",
        reason: str = "",
        authorized_by: str = "operator",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """人工恢复自动交易。仅清除 manual_pause_reason 并回到 TRADING_ENABLED；其他降级原因仍生效。"""

        from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority

        supervisor = self.runtime.supervisor if self.runtime else None
        if supervisor is None:
            return {"status": "failed", "reason": "supervisor_unavailable"}
        phase_before = supervisor.snapshot().phase.value
        previous_pause_reason = supervisor.snapshot().manual_pause_reason
        supervisor.resume_trading()
        snapshot = supervisor.snapshot()
        phase_after = snapshot.phase.value

        trace_id = trace_id or uuid4().hex
        logger.warning(
            "admin resume_trading triggered",
            extra={
                "operator": operator,
                "authorized_by": authorized_by,
                "reason": reason,
                "trace_id": trace_id,
                "phase_before": phase_before,
                "phase_after": phase_after,
            },
        )
        event_bus = self.runtime.event_bus if self.runtime else None
        if event_bus is not None:
            event_bus.publish_nowait(
                OutboxPriority.P1,
                DomainEvent(
                    trace_id=trace_id,
                    event_type=DomainEventType.TRADING_RESUMED,
                    event_id=uuid4().hex,
                    reason=reason or "manual_resume",
                    payload={
                        "operator": operator,
                        "authorized_by": authorized_by,
                        "reason": reason,
                        "phase_before": phase_before,
                        "phase_after": phase_after,
                        "previous_manual_pause_reason": previous_pause_reason,
                        "degraded_reason": snapshot.degraded_reason,
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                    },
                ),
            )
        return {
            "status": "ok",
            "trace_id": trace_id,
            "operator": operator,
            "authorized_by": authorized_by,
            "phase": phase_after,
            "phase_before": phase_before,
            "manual_pause_reason": snapshot.manual_pause_reason,
            "degraded_reason": snapshot.degraded_reason,
            "audit_published": event_bus is not None,
        }

    async def cancel_order(
        self,
        *,
        order_id: str,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        operator: str = "manual",
        reason: str = "admin_cancel_order",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """人工撤单。走 TradingService → OrderExecutor，与策略撤单同一条主链路。"""

        trace_id = trace_id or uuid4().hex
        logger.warning(
            "admin cancel_order triggered",
            extra={
                "operator": operator,
                "reason": reason,
                "order_id": order_id,
                "condition_id": condition_id,
                "trace_id": trace_id,
            },
        )
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

        try:
            trading_service = self._trading_service()
        except RuntimeError as exc:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": str(exc),
                "order": self._serializer().order(source_order),
            }

        strategy_id = self._runtime_strategy_id()
        if not strategy_id:
            return {"status": "failed", "trace_id": trace_id, "reason": "strategy_id_missing"}
        intent = CancelOrderIntent(
            strategy_id=strategy_id,
            trace_id=trace_id,
            condition_id=source_order.condition_id,
            token_id=source_order.token_id,
            order_id=normalize_order_id(source_order),
            market_slug=source_order.market_slug,
            reason=reason,
        )
        review = await trading_service.cancel(intent)
        result = review.order_result
        failed = result is None or result.status in {
            OrderResultStatus.FAILED,
            OrderResultStatus.REJECTED,
        }
        self._publish_order_action_audit(
            event_type_pre="ORDER_CANCEL_REQUESTED",
            event_type_post="ORDER_CANCELLED",
            trace_id=trace_id,
            order=source_order,
            order_result=result,
            operator=operator,
            reason=reason,
        )
        return {
            "status": "failed" if failed else "ok",
            "trace_id": trace_id,
            "operator": operator,
            "reason": "" if result is None else result.reason,
            "order": self._serializer().order(source_order),
            "review": self._serializer().review(review),
        }

    def _publish_order_action_audit(
        self,
        *,
        event_type_pre: str,
        event_type_post: str,
        trace_id: str,
        order: Order,
        order_result: Any,
        operator: str,
        reason: str,
        old_price: Decimal | None = None,
        new_price: Decimal | None = None,
        old_size_shares: Decimal | None = None,
        new_size_shares: Decimal | None = None,
    ) -> None:
        """把 admin 触发的 cancel / replace 落 audit（CLAUDE.md §10 可审计要求）。

        发两个事件：``_REQUESTED`` / ``_SUBMITTED`` 记 admin 意图；``_CANCELLED`` 记
        OrderExecutor 实际返回的结果。两者都必要——worker 路径 (order_result_processor)
        早已发过同名事件；admin 路径之前是漏的。
        """

        from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority

        event_bus = self.runtime.event_bus if self.runtime else None
        if event_bus is None:
            return
        order_id = order.order_id or normalize_order_id(order)
        pre_payload: dict[str, Any] = {
            "operator": operator,
            "reason": reason,
            "order_id": order_id,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        if old_price is not None:
            pre_payload["old_price"] = str(old_price)
        if new_price is not None:
            pre_payload["new_price"] = str(new_price)
        if old_size_shares is not None:
            pre_payload["old_size_shares"] = str(old_size_shares)
        if new_size_shares is not None:
            pre_payload["new_size_shares"] = str(new_size_shares)
        event_bus.publish_nowait(
            OutboxPriority.P1,
            DomainEvent(
                trace_id=trace_id,
                event_type=DomainEventType[event_type_pre],
                event_id=uuid4().hex,
                market_slug=order.market_slug,
                condition_id=order.condition_id,
                token_id=order.token_id,
                reason=reason,
                payload=pre_payload,
            ),
        )
        if order_result is None:
            return
        post_payload = dict(pre_payload)
        post_payload["result_status"] = order_result.status.value if order_result.status else ""
        event_bus.publish_nowait(
            OutboxPriority.P1,
            DomainEvent(
                trace_id=trace_id,
                event_type=DomainEventType[event_type_post],
                event_id=uuid4().hex,
                market_slug=order.market_slug,
                condition_id=order.condition_id,
                token_id=order.token_id,
                reason=order_result.reason or reason,
                payload=post_payload,
            ),
        )

    async def force_exit_position(
        self,
        *,
        condition_id: str,
        token_id: str,
        operator: str = "manual",
        reason: str = "admin_force_exit",
        price: Decimal | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """紧急平仓：用当前 best_bid（或调用方指定价）挂全量 SELL，经 RiskManager → OrderExecutor。"""

        trace_id = trace_id or uuid4().hex
        market = self._resolve_market(condition_id=condition_id, token_id=token_id)
        if market is None:
            return {"status": "failed", "trace_id": trace_id, "reason": "market_not_found"}
        if token_id not in market.token_ids:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "token_not_found",
                "market": self._serializer().market(market),
            }
        if not market_status_allowed_for_manual_order(market):
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "market_not_operable",
                "market": self._serializer().market(market),
            }
        account = self._account_snapshot()
        position = account.get_position(condition_id, token_id)
        if position is None or position.shares <= Decimal("0"):
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "position_not_found",
                "market": self._serializer().market(market),
            }

        orderbook = self._market_ws_snapshot(token_id)
        target_price = price
        if target_price is None:
            if orderbook is None or orderbook.best_bid is None or orderbook.best_bid <= Decimal("0"):
                return {
                    "status": "failed",
                    "trace_id": trace_id,
                    "reason": "best_bid_unavailable",
                    "market": self._serializer().market(market),
                }
            target_price = orderbook.best_bid
        if target_price <= Decimal("0") or target_price >= Decimal("1"):
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "invalid_price",
                "market": self._serializer().market(market),
            }
        # 平仓数量按当前 confirmed shares，扣除已挂的 SELL 数量，避免重复挂单。
        outstanding_sell_shares = Decimal("0")
        for order in account.open_orders_for_market(condition_id, token_id):
            if order.side != OrderSide.SELL:
                continue
            open_shares = order_open_shares(order)
            if open_shares is not None:
                outstanding_sell_shares += open_shares
        exit_shares = position.shares - outstanding_sell_shares
        if exit_shares <= Decimal("0"):
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": "no_exit_shares",
                "market": self._serializer().market(market),
            }

        try:
            trading_service = self._trading_service()
        except RuntimeError as exc:
            return {"status": "failed", "trace_id": trace_id, "reason": str(exc)}

        strategy_id = self._runtime_strategy_id()
        if not strategy_id:
            return {"status": "failed", "trace_id": trace_id, "reason": "strategy_id_missing"}
        intent = SellOrderIntent(
            strategy_id=strategy_id,
            trace_id=trace_id,
            condition_id=condition_id,
            token_id=token_id,
            price=target_price,
            size_shares=exit_shares,
            market_slug=market.market_slug,
            order_type=OrderType.GTC,
        )
        _kelly = self.runtime.strategy.config if self.runtime else CurrentStrategyConfig()
        review = await trading_service.sell(
            intent,
            market=market,
            orderbook=orderbook,
            position=position,
            open_orders=account.open_orders_for_market(condition_id, token_id),
            classification_passed=True,
            balance_usdc=snapshot_available_usdc(account),
            allowance_usdc=snapshot_allowance(account),
            bankroll_usdc=_resolve_admin_bankroll(account, self._settings_value("portfolio_budget_usdc")),
            kelly_max_position_fraction=_kelly.kelly_max_position_fraction,
            kelly_round_up_max_overbet_ratio=_kelly.kelly_round_up_max_overbet_ratio,
        )
        result = review.order_result
        failed = result is None or result.status in {
            OrderResultStatus.FAILED,
            OrderResultStatus.REJECTED,
        }
        return {
            "status": "failed" if failed else "ok",
            "trace_id": trace_id,
            "operator": operator,
            "reason": (
                review.risk_decision.reason
                if review.risk_decision is not None and not review.risk_decision.passed
                else ("" if result is None else result.reason)
            ),
            "market": self._serializer().market(market),
            "position": self._serializer().position(position),
            "review": self._serializer().review(review),
        }

    async def pause_market_manual(
        self,
        *,
        condition_id: str,
        reason: str = "manual_pause",
        operator: str = "manual",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """人工暂停某市场。account_state.market_pauses 写入 source=MANUAL，阻止入场。

        trace_id 透传到返回 dict 让前端串审计；当前未在此处显式发 audit event
        (依赖 account_state 内部变更通道)，未来补独立 trading_paused_for_market
        事件时直接消费此参数。"""

        account_state = self.runtime.account_state_store if self.runtime else None
        if account_state is None:
            return {"status": "failed", "reason": "account_state_store_unavailable"}
        normalized_reason = reason.strip() or "manual_pause"
        snapshot = account_state.pause_market(
            condition_id,
            reason=normalized_reason,
            source=MarketPauseSource.MANUAL,
            recoverable=True,
        )
        pause = snapshot.pause_for_market(condition_id)
        return {
            "status": "ok",
            "trace_id": trace_id or uuid4().hex,
            "operator": operator,
            "condition_id": condition_id,
            "reason": normalized_reason,
            "pause": None if pause is None else pause.as_payload(),
        }

    async def resume_market_manual(
        self,
        *,
        condition_id: str,
        operator: str = "manual",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """人工恢复某市场。仅清除 market_pauses 中对应条目。"""

        account_state = self.runtime.account_state_store if self.runtime else None
        if account_state is None:
            return {"status": "failed", "reason": "account_state_store_unavailable"}
        account_state.resume_market(condition_id)
        _ = trace_id  # 透传字段：当前未参与持久化，未来 trading_resumed_for_market 事件直接消费
        return {
            "status": "ok",
            "operator": operator,
            "condition_id": condition_id,
        }

    async def bulk_cancel_orders(
        self,
        *,
        order_ids: Sequence[str],
        operator: str = "manual",
        reason: str = "admin_bulk_cancel",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """批量撤单：对列表里每个 order_id 依次调用 cancel_order。

        每笔撤单独立走 TradingService → OrderExecutor，有独立 trace_id。
        失败不影响后续条目——全量跑完后返回汇总结果。
        上限 20 单，防止一次 admin 操作占用交易服务过久。
        """

        logger.warning(
            "admin bulk_cancel_orders triggered",
            extra={
                "operator": operator,
                "reason": reason,
                "order_count": len(order_ids),
                "trace_id": trace_id,
            },
        )
        results = []
        succeeded = 0
        failed = 0

        for oid in order_ids[:20]:
            item_trace = uuid4().hex
            result = await self.cancel_order(
                order_id=oid,
                operator=operator,
                reason=reason,
                trace_id=item_trace,
            )
            status = result.get("status", "failed")
            if status == "ok":
                succeeded += 1
            else:
                failed += 1
            results.append({
                "order_id": oid,
                "trace_id": item_trace,
                "status": status,
                "reason": result.get("reason", ""),
            })

        return {
            "submitted": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "operator": operator,
            "parent_trace_id": trace_id,
            "results": results,
        }


