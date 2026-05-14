from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents

from polymarket_trader.extension_api.manifest import ConfiguredExtension

from polymarket_trader.app.admin_order_control import AdminOrderController
from polymarket_trader.app.admin_runtime_view import AdminRuntimeView
from polymarket_trader.app.admin_serialization import AdminSerializer, decimal_text, jsonable
from polymarket_trader.app.admin_service_helpers import (
    _RepositoryGroup,
)
from polymarket_trader.app.order_projection import AccountStateProjector, normalize_order_id
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.extension_api.manual_confirmation import ManualConfirmation
from polymarket_trader.workers.trading_decision import (
    TRADING_DECISION_WORKER_ORIGIN,
    serialize_allocation,
    serialize_allocation_plan,
    serialize_intent,
    serialize_plan_metadata,
    serialize_review,
)
from polymarket_trader.infra.db import (
    AllocationRepository,
    AuditEventRepository,
    DecisionRecordRepository,
    FillRepository,
    MarketRepository,
    OrderRepository,
    OrderbookSnapshotRepository,
    OutboxEventRepository,
    PositionRepository,
    RepositoryPage,
)
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.app.admin_controls_mixin import AdminControlsMixin
from polymarket_trader.app.admin_query import AdminQueryMixin


@dataclass(frozen=True, slots=True)
class AdminService(AdminQueryMixin, AdminControlsMixin):
    """Coordinates read-only admin queries and controlled manual operations.

    Read methods 来自 ``AdminQueryMixin``；受控操作来自 ``AdminControlsMixin``；
    私有 helper（_serializer / _runtime_view / _resolve_market / ...）保留在本类内。
    """

    runtime: RuntimeComponents | None = None

    def bind_runtime(self, runtime: RuntimeComponents) -> "AdminService":
        return AdminService(runtime=runtime)

    def _serializer(self) -> AdminSerializer:
        return AdminSerializer(
            account_snapshot_provider=self._account_snapshot,
            registry_snapshot_provider=self._registry_snapshot,
            market_ws_snapshot=self._market_ws_snapshot,
        )

    def _runtime_view(self) -> AdminRuntimeView:
        return AdminRuntimeView(runtime=self.runtime)

    def _order_controller(self) -> AdminOrderController:
        strategy_id = self._runtime_strategy_id()
        if not strategy_id:
            raise RuntimeError("runtime extension missing strategy_id")
        return AdminOrderController(
            runtime=self.runtime,
            strategy_id=strategy_id,
            serializer=self._serializer(),
            account_snapshot=self._account_snapshot,
            resolve_market=self._resolve_market,
            trading_service=self._trading_service,
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
        # settings 缺失时执行。kelly_* 从策略侧 ConfiguredExtension.config 读取；
        # 策略配置是 kelly_* 的唯一真相来源，不再走框架 Settings。
        settings = self.runtime.settings
        extension = self.runtime.extension
        from strategies.current.config import CurrentStrategyConfig as _CurrentStrategyConfig
        strategy_config: _CurrentStrategyConfig = (
            extension.config if isinstance(extension, ConfiguredExtension) else _CurrentStrategyConfig()
        )
        return self._trading_decision_service().build_entry_plan(
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
            kelly_drawdown_halt_fraction=strategy_config.kelly_drawdown_halt_fraction,
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
        return {
            "candidate_id": f"{plan.trace_id}:{market.condition_id}:{token_id}",
            "strategy_id": self._runtime_strategy_id(),
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
        }

    def _runtime_strategy_id(self) -> str | None:
        """读取当前运行时加载的扩展策略 id，供候选过滤等内存视图使用。"""

        if self.runtime is None:
            return None
        try:
            extension = self.runtime.extension
        except RuntimeError:
            return None
        spec = getattr(extension, "spec", None)
        return getattr(spec, "strategy_id", None)

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
        """候选展示只读取已形成运行时事实的市场，不用页面请求触发全量业务评估。"""

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
                markets[market.condition_id] = market
        return tuple(markets.values())

    def _project_manual_entry_result(self, review, *, snapshot: AccountSnapshot) -> None:
        account_state = self.runtime.account_state_store if self.runtime else None
        if account_state is None or review.order_result is None:
            return
        strategy_id = self._runtime_strategy_id()
        if not strategy_id:
            raise RuntimeError("runtime extension missing strategy_id")
        projector = AccountStateProjector(account_state, strategy_id=strategy_id)
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

    def _has_db_session_factory(self) -> bool:
        return self.runtime is not None and self.runtime.db_session_factory is not None

    def _market_ws_snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        worker = self.runtime.market_ws_worker if self.runtime else None
        return worker.snapshot(token_id) if worker is not None else None

    def _clob_client(self) -> Any:
        if self.runtime is None:
            raise RuntimeError("clob_client unavailable")
        return self.runtime.clob_client

    def _trading_service(self) -> TradingService:
        if self.runtime is None:
            raise RuntimeError("trading_service unavailable")
        return self.runtime.trading_service

    def _trading_decision_service(self) -> TradingDecisionService:
        if self.runtime is None:
            raise RuntimeError("trading_decision_service unavailable")
        return self.runtime.trading_decision_service

    def _entry_metadata_store(self) -> Any | None:
        return self.runtime.entry_metadata_store if self.runtime else None

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

    async def _with_repositories(self, callback: Callable[[_RepositoryGroup], Any]) -> Any:
        session_factory = self.runtime.db_session_factory if self.runtime else None
        if session_factory is None:
            raise RuntimeError("db_session_factory unavailable")
        async with session_factory() as session:
            repositories = _RepositoryGroup(
                audit=AuditEventRepository(session),
                market=MarketRepository(session),
                order=OrderRepository(session),
                fill=FillRepository(session),
                position=PositionRepository(session),
                allocation=AllocationRepository(session),
                decision=DecisionRecordRepository(session),
                outbox=OutboxEventRepository(session),
                orderbook=OrderbookSnapshotRepository(session),
            )
            return await callback(repositories)

    def _slice_sequence(
        self,
        items: Sequence[Any],
        *,
        limit: int,
        offset: int,
    ) -> RepositoryPage[Any]:
        if limit <= 0:
            limit = 100
        if offset < 0:
            offset = 0
        sliced = tuple(items[offset : offset + limit])
        return RepositoryPage(items=sliced, total=len(items), limit=limit, offset=offset)

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

