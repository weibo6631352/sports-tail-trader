from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents


from polymarket_trader.app.admin_order_control import AdminOrderController
from polymarket_trader.serialization import decimal_text, jsonable
from polymarket_trader.api.serialization import AdminSerializer
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
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.app.admin_controls_mixin import AdminControlsMixin


@dataclass(frozen=True, slots=True)
class AdminService(AdminControlsMixin):
    """Coordinates controlled manual operations.

    受控操作来自 ``AdminControlsMixin``；本类只保留 helper（_serializer /
    _resolve_market / _account_snapshot / ...）供 mixin 调用。所有只读查询
    已迁到 ``api/aggregators/*``，本类不再装查询逻辑。
    """

    runtime: RuntimeComponents | None = None

    def bind_runtime(self, runtime: RuntimeComponents) -> "AdminService":
        return AdminService(runtime=runtime)

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

    def _serializer(self) -> AdminSerializer:
        return AdminSerializer(
            account_snapshot_provider=self._account_snapshot,
            registry_snapshot_provider=self._registry_snapshot,
            market_ws_snapshot=self._market_ws_snapshot,
        )

    def _order_controller(self) -> AdminOrderController:
        return AdminOrderController(
            runtime=self.runtime,
            serializer=self._serializer(),
            account_snapshot=self._account_snapshot,
            resolve_market=self._resolve_market,
            order_gateway=self._order_gateway,
            find_open_order=self._find_open_order,
        )

    def _build_entry_plan_for_admin(
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
        return self._decision_builder().build_entry_plan(
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
                    "origin": "admin_candidate_confirm",
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

