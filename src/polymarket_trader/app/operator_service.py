from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents


from polymarket_trader.app.operator_order_control import OperatorOrderController
from polymarket_trader.serialization import decimal_text, jsonable
from polymarket_trader.api.serialization import ApiSerializer
from polymarket_trader.app.order_projection import AccountStateProjector, normalize_order_id
from polymarket_trader.pipeline.decision.decision_context_builder import DecisionContextBuilder
from polymarket_trader.pipeline.execution.order_gateway import OrderGateway
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.decisions import ManualConfirmation
from polymarket_trader.pipeline.decision.serialization import (
    TRADING_DECISION_WORKER_ORIGIN,
    serialize_allocation,
    serialize_allocation_plan,
    serialize_intent,
    serialize_plan_metadata,
    serialize_review,
)
from polymarket_trader.runtime.registry import MarketRegistrySnapshot

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.app.order_projection import order_open_shares
from polymarket_trader.domain.account import AccountSnapshot, MarketPauseSource
from polymarket_trader.domain.market import (
    market_status_allowed_for_manual_order,
    normalize_condition_ids,
)
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    OrderResultStatus,
    OrderSide,
    OrderType,
    SellOrderIntent,
)
from polymarket_trader.pipeline.decision.serialization import (
    snapshot_allowance,
    snapshot_available_usdc,
)
from polymarket_trader.workflow.config import TradingWorkflowConfig

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


@dataclass(frozen=True, slots=True)
class OperatorService:
    """运维人工触发的受控操作（reconcile / pause / replace / cancel /
    confirm / settle / live-state 维护等），所有方法仍走统一交易主链路
    （OrderGateway → RiskManager → OrderExecutor），不绕过。

    只读查询已全部迁到 `api/aggregators/*`；本类只保留写动作 + 助手方法。
    """

    runtime: RuntimeComponents | None = None

    def bind_runtime(self, runtime: RuntimeComponents) -> "OperatorService":
        return OperatorService(runtime=runtime)

    def get_position_signals(
        self, *, condition_id: str | None = None, token_id: str | None = None
    ) -> dict[str, object]:
        """返回最近一次 decide_exit 决策的完整持仓信号快照。

        命名"持仓信号"而非"exit 决策信号"——内部 metadata 仍用 dynamic_exit_*
        前缀保持 audit/测试兼容，对外接口语义是持仓评估的多信号 breakdown：
        5 类投票（fair_value/imbalance/best_bid/goalserve/math_lock）+ 流动性
        tier + math_lock 是否支持 + fair_value 来源等。

        condition_id / token_id 都缺 → 返回全部持仓 signals 列表。
        """

        if self.runtime is None or self.runtime.market_tick_worker is None:
            return {"items": []}
        cache = self.runtime.market_tick_worker._token_position_signals
        items: list[dict[str, object]] = []
        for tid, signals in cache.items():
            if token_id is not None and tid != token_id:
                continue
            if condition_id is not None and signals.get("condition_id") != condition_id:
                continue
            items.append(signals)
        return {"items": items, "total": len(items)}

    def _serializer(self) -> ApiSerializer:
        return ApiSerializer(
            account_snapshot_provider=self._account_snapshot,
            registry_snapshot_provider=self._registry_snapshot,
            market_ws_snapshot=self._market_ws_snapshot,
        )

    def _order_controller(self) -> OperatorOrderController:
        return OperatorOrderController(
            runtime=self.runtime,
            serializer=self._serializer(),
            account_snapshot=self._account_snapshot,
            resolve_market=self._resolve_market,
            order_gateway=self._order_gateway,
            find_open_order=self._find_open_order,
        )

    def _build_trade_plan_for_operator(
        self,
        *,
        market: Market,
        token_id: str,
        orderbook: OrderbookSnapshot,
        account: AccountSnapshot,
        trace_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        manual_confirmation: "ManualConfirmation | None" = None,
    ):
        # runtime.settings 是 main.py 启动后绑定的强字段——admin 路径不可能在
        # settings 缺失时执行。kelly_* 从策略侧 TradingWorkflow.config 读取；
        # 策略配置是 kelly_* 的唯一真相来源，不再走框架 Settings。
        settings = self.runtime.settings
        strategy_config = self.runtime.workflow.config
        return self._decision_builder().build_trade_plan(
            market=market,
            orderbook=orderbook,
            # 候选投影和人工确认属于受控操作入口，不使用自动入场开关截断候选生成；
            # 仓位、挂单、余额仍显式传入，并在确认提交前继续经过 RiskManager。
            account_snapshot=None,
            token_id=token_id,
            trace_id=trace_id,
            portfolio_budget_usdc=settings.portfolio_budget_usdc,
            available_usdc=account.available_usdc,
            kelly_fraction=strategy_config.kelly_fraction,
            kelly_max_position_fraction=strategy_config.kelly_max_position_fraction,
            kelly_min_edge=strategy_config.kelly_min_edge,
            kelly_min_stake_usdc=strategy_config.kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=strategy_config.kelly_allow_round_up_to_market_min,
            kelly_round_up_max_overbet_ratio=strategy_config.kelly_round_up_max_overbet_ratio,
            positions=account.positions,
            open_orders=account.open_orders,
            metadata=metadata if metadata is not None else self._entry_metadata_for_market(market),
            manual_confirmation=manual_confirmation,
        )

    def _candidate_payload(self, market: Market, token_id: str, plan) -> dict[str, Any]:
        outcome = market.get_outcome_by_token_id(token_id)
        summary = plan.summary
        extras = dict(summary.extras) if summary is not None else {}
        execution_permission = extras.get("execution_permission") if extras else None
        strategy_action = summary.action if summary is not None else ""
        # 策略想 auto_execute 但 plan 因 framework 风控 / 资金 / 盘口被挡住时，
        # admin 展示统一标 reject，让运营能区分"策略主动拒绝"vs"被框架挡住"。
        if strategy_action == "auto_execute" and not plan.ready_to_trade:
            action_label = "reject"
            block_reason = ""
            allocation = plan.allocation
            if allocation is not None:
                block_reason = str(allocation.release_reason or allocation.reason or "")
            reason_text = block_reason or plan.reason or (summary.reason if summary is not None else "")
        else:
            action_label = strategy_action
            reason_text = (summary.reason if summary is not None else "") or plan.reason or ""
        accepted = bool(action_label and action_label != "reject")
        confirmable = (
            execution_permission == "manual_confirm"
            and action_label == "manual_confirm"
            and not (summary.manual_confirmed if summary is not None else False)
        )
        # 从 market_metadata_store 反查该市场的直播源信号状态,让 candidate
        # 一次性带出"为什么被拒"的上游信息(数据源是否给出 signal_allowed).
        signal_allowed: bool | None = None
        live_state_age_ms: int | None = None
        live_state_source: str | None = None
        try:
            meta_store = self._entry_metadata_store()
            if meta_store is not None:
                meta_rec = next(
                    (r for r in meta_store.records() if r.condition_id == market.condition_id),
                    None,
                )
                if meta_rec is not None:
                    signal_allowed = meta_rec.live_state_signal_allowed
                    live_state_source = meta_rec.source
                    from datetime import datetime, timezone
                    if meta_rec.updated_at is not None:
                        live_state_age_ms = int(
                            (datetime.now(timezone.utc) - meta_rec.updated_at).total_seconds() * 1000
                        )
        except Exception:
            pass
        return {
            "candidate_id": f"{plan.trace_id}:{market.condition_id}:{token_id}",
            "trace_id": plan.trace_id,
            "condition_id": market.condition_id,
            "market_slug": market.market_slug,
            "event_slug": market.event_slug,
            "event_title": market.event_title,
            "token_id": token_id,
            "outcome": None if outcome is None else outcome.outcome,
            "ready_to_trade": plan.ready_to_trade,
            "accepted": accepted,
            "confirmable": confirmable,
            "decision_kind": None if plan.decision_kind is None else plan.decision_kind.value,
            "reason": reason_text,
            "action": action_label,
            "strategy_action": strategy_action,
            "execution_permission": execution_permission,
            "label": summary.label if summary is not None else "",
            "market_type": summary.market_type if summary is not None else "",
            "side": summary.side if summary is not None else "",
            "line": (
                decimal_text(summary.line) if summary is not None and summary.line is not None else None
            ),
            "best_ask": (
                decimal_text(summary.best_ask) if summary is not None and summary.best_ask is not None else None
            ),
            "manual_confirmed": summary.manual_confirmed if summary is not None else False,
            "confirmed_by": summary.confirmed_by if summary is not None else "",
            "confirm_reason": summary.confirm_reason if summary is not None else "",
            "extras": jsonable(extras),
            "allocation": None if plan.allocation is None else {
                "target_budget_usdc": decimal_text(plan.allocation.target_budget_usdc),
                "buy_budget_usdc": decimal_text(plan.allocation.buy_budget_usdc),
                "reason": plan.allocation.reason,
                "release_reason": plan.allocation.release_reason,
            },
            "intent": None if plan.intent is None else serialize_intent(plan.intent),
            "payload": jsonable(plan.metadata or {}),
            # 上游直播源信号状态(从 entry_metadata 反查),让 candidate 自带 "为什么没/被信号支持"
            "signal_allowed": signal_allowed,
            "live_state_age_ms": live_state_age_ms,
            "live_state_source": live_state_source,
        }

    def _entry_metadata_for_market(self, market: Market) -> dict[str, Any]:
        store = self._entry_metadata_store()
        if store is None:
            return {}
        return store.metadata_for(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
        )

    def _candidate_source_markets(
        self,
        *,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> tuple[Market, ...]:
        """候选展示只读取已形成运行时事实的市场，不用页面请求触发全量业务评估。

        list-all 模式 (无单查参数) 额外过滤 ``market_outside_trade_window``,
        跟 polymarket 自己的 /sports/live 同口径只取"即将开赛 30min ~ 已开赛 6h"
        窗口内的 single-game 市场——避免 market_metadata_store 累积的已结束 stale
        records 拖累每次 /candidates 评估 (实测 956 records 中绝大多数已过窗口).
        单查模式不过滤, 人查可能需要看已结束市场.
        """

        if condition_id is not None or token_id is not None or market_slug is not None:
            market = self._resolve_market(
                condition_id=condition_id,
                token_id=token_id,
                market_slug=market_slug,
            )
            return () if market is None else (market,)

        store = self._entry_metadata_store()
        registry = self.runtime.registry if self.runtime else None
        if store is None or registry is None:
            return ()

        from datetime import datetime, timezone
        from polymarket_trader.app.market_tracking_policy import market_outside_trade_window
        now = datetime.now(timezone.utc)

        markets: dict[str, Market] = {}
        for record in store.records():
            # 候选读取两类 record：
            # 1) live_state_payload 非空——single_game 主路径（sports_live_state_worker 写入）
            # 2) metadata 含 series_state / game_odds / season_odds_snapshot——series winner /
            #    outright 类市场由专用 worker 写入 metadata，不经 live_state hook。
            _has_series_data = bool(
                record.metadata.get("series_state")
                or record.metadata.get("game_odds")
                or record.metadata.get("season_odds_snapshot")
            )
            if not record.live_state_payload and not _has_series_data:
                continue
            market = None
            if record.condition_id:
                market = registry.get_by_condition_id(record.condition_id)
            if market is None and record.market_slug:
                market = registry.get_by_slug(record.market_slug)
            if market is None and record.event_slug:
                market = registry.get_by_slug(record.event_slug)
            if market is not None:
                # 时间窗过滤: outright/futures (无 game_start_time) 返 False 保留;
                # 远期未开赛 30min+ / 早已结束 6h+ 的 single-game 直接跳过.
                if market_outside_trade_window(market, now=now):
                    continue
                markets[market.condition_id] = market
        return tuple(markets.values())

    def _project_manual_entry_result(self, review, *, snapshot: AccountSnapshot) -> None:
        account_state = self.runtime.account_state_store if self.runtime else None
        if account_state is None or review.order_result is None:
            return
        projector = AccountStateProjector(account_state)
        projector.apply_buy_result(review.order_result, snapshot=snapshot)
        projector.apply_result_flags(review.order_result, snapshot=snapshot)

    async def _publish_candidate_confirmation_review(self, *, market: Market, plan, review) -> None:
        event_bus = self.runtime.event_bus if self.runtime else None
        if event_bus is None or plan.intent is None:
            return
        await event_bus.publish(
            OutboxPriority.P3,
            DomainEvent(
                trace_id=plan.trace_id,
                event_type=(
                    DomainEventType.RISK_CHECK_PASSED
                    if review.risk_decision is not None and review.risk_decision.passed
                    else DomainEventType.RISK_CHECK_FAILED
                ),
                event_id=uuid4().hex,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
                condition_id=market.condition_id,
                token_id=plan.intent.token_id,
                reason="risk_decision_unavailable" if review.risk_decision is None else review.risk_decision.reason,
                payload={
                    "origin": "operator_candidate_confirm",
                    "entry_origin": TRADING_DECISION_WORKER_ORIGIN,
                    "operator": jsonable(plan.summary.confirmed_by if plan.summary is not None else ""),
                    "confirm_reason": jsonable(plan.summary.confirm_reason if plan.summary is not None else ""),
                    "allocation_plan": serialize_allocation_plan(plan),
                    "allocation": serialize_allocation(plan),
                    "plan_metadata": serialize_plan_metadata(plan),
                    "intent": serialize_intent(plan.intent),
                    "review": serialize_review(review),
                },
            ),
        )

    def _account_snapshot(self) -> AccountSnapshot:
        store = self.runtime.account_state_store if self.runtime else None
        return store.snapshot() if store is not None else AccountSnapshot()

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        registry = self.runtime.registry if self.runtime else None
        return registry.snapshot() if registry is not None else MarketRegistrySnapshot(tuple())

    def _market_ws_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        worker = self.runtime.market_ws_worker if self.runtime else None
        return worker.snapshot(token_id) if worker is not None else None

    def _clob_client(self) -> Any:
        if self.runtime is None:
            raise RuntimeError("clob_client unavailable")
        return self.runtime.clob_client

    def _order_gateway(self) -> OrderGateway:
        if self.runtime is None:
            raise RuntimeError("order_gateway unavailable")
        return self.runtime.order_gateway

    def _decision_builder(self) -> DecisionContextBuilder:
        if self.runtime is None:
            raise RuntimeError("decision_context_builder unavailable")
        return self.runtime.decision_context_builder

    def _entry_metadata_store(self) -> Any | None:
        return self.runtime.market_metadata_store if self.runtime else None

    def _settings_value(self, name: str) -> Any:
        settings = self.runtime.settings if self.runtime else None
        return None if settings is None else getattr(settings, name, None)

    def _resolve_market(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Market | None:
        registry = self.runtime.registry if self.runtime else None
        if registry is not None:
            if condition_id is not None:
                market = registry.get_by_condition_id(condition_id)
                if market is not None:
                    return market
            if token_id is not None:
                market = registry.get_by_token_id(token_id)
                if market is not None:
                    return market
            if market_slug is not None:
                market = registry.get_by_slug(market_slug)
                if market is not None:
                    return market
        return None

    def _find_open_order(
        self,
        snapshot: AccountSnapshot,
        *,
        order_id: str,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Order | None:
        matches = [
            order
            for order in snapshot.open_orders
            if order.open
            and order_id in {normalize_order_id(order), order.order_id, order.idempotency_key}
            and (market_slug is None or order.market_slug == market_slug)
            and (condition_id is None or order.condition_id == condition_id)
            and (token_id is None or order.token_id == token_id)
        ]
        if len(matches) != 1:
            return None
        return matches[0]


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
        candidate_plan = self._build_trade_plan_for_operator(
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
        plan = self._build_trade_plan_for_operator(
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

        _kelly = self.runtime.workflow.config if self.runtime else TradingWorkflowConfig()
        review = await self._order_gateway().review_intent(
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
        """人工撤单。走 OrderGateway → OrderExecutor，与策略撤单同一条主链路。"""

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
            order_gateway = self._order_gateway()
        except RuntimeError as exc:
            return {
                "status": "failed",
                "trace_id": trace_id,
                "reason": str(exc),
                "order": self._serializer().order(source_order),
            }

        intent = CancelOrderIntent(
            trace_id=trace_id,
            condition_id=source_order.condition_id,
            token_id=source_order.token_id,
            order_id=normalize_order_id(source_order),
            market_slug=source_order.market_slug,
            reason=reason,
        )
        review = await order_gateway.cancel(intent)
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
            order_gateway = self._order_gateway()
        except RuntimeError as exc:
            return {"status": "failed", "trace_id": trace_id, "reason": str(exc)}

        intent = SellOrderIntent(
            trace_id=trace_id,
            condition_id=condition_id,
            token_id=token_id,
            price=target_price,
            size_shares=exit_shares,
            market_slug=market.market_slug,
            order_type=OrderType.GTC,
        )
        review = await order_gateway.sell(
            intent,
            market=market,
            position=position,
            balance_usdc=snapshot_available_usdc(account),
            allowance_usdc=snapshot_allowance(account),
            bankroll_usdc=_resolve_admin_bankroll(account, self._settings_value("portfolio_budget_usdc")),
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

        每笔撤单独立走 OrderGateway → OrderExecutor，有独立 trace_id。
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

