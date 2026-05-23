from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

if TYPE_CHECKING:
    from polymarket_trader.app.parameter_store import ParameterStore
from uuid import uuid4

from polymarket_trader.app.trading_decision_service import EntryPlan, TradingDecisionService
from polymarket_trader.app.trading_service import TradingReviewResult, TradingService
from polymarket_trader.app.order_projection import AccountStateProjector
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    ManagedOrderIntent,
    Order,
    OrderResult,
    OrderResultStatus,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.state_machine import MarketLifecycle
from polymarket_trader.domain.account import AccountSnapshot, MarketPauseSource
from polymarket_trader.extension_api import ExtensionAction, ExtensionContext, MarketTokenView
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from .event_payloads import (
    TRADING_DECISION_WORKER_ORIGIN,
    coerce_order_result_from_event,
    is_self_emitted,
    market_from_result,
    serialize_allocation,
    serialize_allocation_plan,
    serialize_intent,
    serialize_plan_metadata,
    serialize_review,
    serialize_snapshot,
    snapshot_allowance,
    snapshot_available_usdc,
    snapshot_position,
)
from .order_result_processor import TradingOrderResultProcessor
from .result import TradingDecisionWorkerResult

PositionsProvider = Callable[[], Iterable[Position]]
OpenOrdersProvider = Callable[[], Iterable[Order]]
EntryMetadataProvider = Callable[[DomainEvent, AccountSnapshot | None], Mapping[str, object] | None]
HeartbeatCallback = Callable[..., None]

# Worker 长时间空闲等事件时，仍要定期触发 heartbeat 让 supervisor 区分「卡死」和「空等」。
# 60s 在 N13 观测的"4 分钟无心跳"上下文里足够灵敏，又不会刷屏。
logger = logging.getLogger(__name__)

_TRADING_DECISION_IDLE_HEARTBEAT_SECONDS = 60.0
# _lifecycle_timeline LRU 上限：跟踪市场数超过此值时淘汰最久未更新的条目。
# 每个市场最多保留最近 _LIFECYCLE_HISTORY_PER_MARKET 次转换记录。
_LIFECYCLE_MARKET_CAP = 500
_LIFECYCLE_HISTORY_PER_MARKET = 50
# allocation_decision_recorded 高频事件 dedup 上限：(condition_id, token_id) → state-hash。
# orderbook 每秒上百条更新都跑分配决策；同一市场 candidate 集合 + reason + 是否拿到
# buy budget 没变时不再 emit，避免 audit_events 每天千万条。precise buy_budget_usdc
# 随 bankroll/orderbook 每 tick 抖动，不进 hash——只看"是否真的拿到预算"这个布尔位。
_ALLOCATION_DEDUPE_CAPACITY = 10_000

POSITION_INCREASE_LIFECYCLES = {
    MarketLifecycle.POSITION_OPEN,
    MarketLifecycle.FOLLOW_UP_ORDER_OPEN,
}
ENTRY_ATTEMPT_LIFECYCLES = {
    MarketLifecycle.WATCHING_ORDERBOOK,
    MarketLifecycle.ENTRY_READY,
    # ENTRY_REJECTED 也允许重试：核心哲学是"失败积极重试"（§17），上一次失败后
    # 不能永久死锁该 market——盘口/edge/账户敞口随时变化，新 ENTRY_SIGNAL 触发
    # 时应让风控重新评估。trading_service.review_intent 每次都拉最新
    # snapshot.open_orders，发现已存在 open BUY 会直接拒（避免重复下单副作用），
    # 所以信任风控层防重入，不靠 lifecycle 死锁来"保护"。原"等 reconcile/人工"
    # 设计让 KBO odds_gap 8 次 allocation accept $20-29 budget 0 单——把 ENTRY
    # 路径堵死的代价远大于偶发额外评估开销。
    MarketLifecycle.ENTRY_REJECTED,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _entry_gate_closed_for_event(
    snapshot: AccountSnapshot | None,
    event: DomainEvent,
) -> bool:
    """账户或单市场入场闸门关闭时，不对高频盘口事件构建交易计划。"""

    if snapshot is None:
        return False
    if not snapshot.allow_new_entries:
        return True
    if event.condition_id and snapshot.is_market_paused(event.condition_id):
        return True
    return False


class TradingDecisionWorker:
    priority = "P0"

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        trading_decision_service: TradingDecisionService | None = None,
        trading_service: TradingService | None = None,
        positions_provider: PositionsProvider | None = None,
        open_orders_provider: OpenOrdersProvider | None = None,
        account_state_store: AccountStateStore | None = None,
        portfolio_budget_usdc: Decimal = Decimal("0"),
        kelly_fraction: Decimal = Decimal("0.25"),
        kelly_max_position_fraction: Decimal = Decimal("0.10"),
        kelly_min_edge: Decimal = Decimal("0.02"),
        kelly_min_stake_usdc: Decimal = Decimal("1"),
        kelly_allow_round_up_to_market_min: bool = True,
        kelly_round_up_max_overbet_ratio: Decimal = Decimal("1"),
        order_retry_limit: int | None = None,
        entry_metadata_provider: EntryMetadataProvider | None = None,
        orderbook_direction_signal_reader: "Callable[..., Any] | None" = None,
        parameter_store: "ParameterStore | None" = None,
        heartbeat: HeartbeatCallback | None = None,
        idle_heartbeat_seconds: float = _TRADING_DECISION_IDLE_HEARTBEAT_SECONDS,
    ) -> None:
        self._event_bus = event_bus
        if trading_decision_service is None:
            raise ValueError("trading_decision_service is required")
        self._trading_decision_service = trading_decision_service
        self._trading_service = trading_service or TradingService()
        self._account_state_store = account_state_store
        self._positions_provider = positions_provider or self._build_positions_provider()
        self._open_orders_provider = open_orders_provider or self._build_open_orders_provider()
        # 静态启动值（来自 Settings）；运行时通过 ``parameter_store`` 的 override
        # 覆盖。每次 review 前用 property 读出当前值——这样 agent PUT 后立刻生效。
        self._portfolio_budget_usdc_default = portfolio_budget_usdc
        self._kelly_fraction_default = kelly_fraction
        self._kelly_max_position_fraction_default = kelly_max_position_fraction
        self._kelly_min_edge_default = kelly_min_edge
        self._kelly_min_stake_usdc_default = kelly_min_stake_usdc
        self._kelly_allow_round_up_default = kelly_allow_round_up_to_market_min
        self._kelly_round_up_max_overbet_ratio_default = kelly_round_up_max_overbet_ratio
        self._order_retry_limit_default = order_retry_limit
        self._parameter_store = parameter_store
        self._entry_metadata_provider = entry_metadata_provider
        # 注入 OrderbookDeltaStore.direction_signal callable（保留 layering：
        # worker 不直接 import runtime/orderbook_delta 类型，仅通过 callable 取 dict）。
        self._orderbook_direction_signal_reader = orderbook_direction_signal_reader
        # supervisor 注入的轻量回调；worker 不直接持有 Supervisor，避免 P0 模块反向耦合到 runtime。
        self._heartbeat = heartbeat
        # 单测可以设 < 1s 让 idle heartbeat 路径快速触发；运行时仍用 60s 默认。
        self._idle_heartbeat_seconds = max(0.001, float(idle_heartbeat_seconds))
        self._market_lifecycle: dict[str, MarketLifecycle] = {}
        # token_id → 上次 *真实* ORDERBOOK_SNAPSHOT_UPDATED 事件处理时间戳。
        # main.py position_exit_evaluator 5s 周期 publish 合成 event，但合成
        # 事件处理时检查：该 token 5s 内已有真实 event 就 skip，避免重复评估。
        self._token_last_real_orderbook_at: dict[str, datetime] = {}
        # token_id → 最近一次 decide_exit 决策的完整 metadata 快照。
        # /positions/signals admin endpoint 从此读取，给 UI/操盘人实时展示
        # 5 类投票 + 流动性 tier + math_lock 是否支持 + fair_value 来源。
        # 仅持仓 token 写入，无持仓 token 不会有 entry，自动 LRU 由内存压力管理。
        self._token_position_signals: dict[str, dict[str, Any]] = {}
        # (strategy_id, reason) → count，供 admin/observability 查询哪个策略因何跳过了多少次。
        self._skip_reason_histogram: dict[tuple[str, str], int] = {}
        # condition_id → [(lifecycle, timestamp), …]，记录每次状态转换的时间点。
        # OrderedDict + cap = LRU 防止无限增长（见 _LIFECYCLE_MARKET_CAP）。
        self._lifecycle_timeline: OrderedDict[str, list[tuple[MarketLifecycle, datetime]]] = OrderedDict()
        # allocation_decision_recorded dedup：(condition_id, token_id) → 上次 emit 的
        # state-hash。同一 key 状态未变跳过 publish，依然计入 _suppressed_allocation_emits。
        self._last_allocation_state_hash: OrderedDict[tuple[str | None, str | None], str] = OrderedDict()
        self._suppressed_allocation_emits: int = 0
        self._order_result_processor = TradingOrderResultProcessor(
            host=self,
            trading_decision_service=self._trading_decision_service,
            trading_service=self._trading_service,
            account_state_store=self._account_state_store,
        )

    def _fetch_orderbook_direction(self, token_id: str | None) -> dict[str, Any] | None:
        """从 OrderbookDeltaStore 取 10s 窗口方向信号，序列化成 dict 注入
        ExtensionContext.metadata['orderbook_direction']。

        策略消费归一化复合信号（direction_score / price_momentum / flow_imbalance /
        direction_label / confidence），替代单时点 bid/ask 深度比 imbalance ratio——
        后者会被 MM 假墙骗，flow_imbalance 是窗口内 best 价位移 + real_depth 消耗
        的真实订单流方向，更可靠。
        """
        if self._orderbook_direction_signal_reader is None or not token_id:
            return None
        try:
            signal = self._orderbook_direction_signal_reader(token_id, window_seconds=10.0)
        except Exception:
            return None
        if signal is None:
            return None
        as_metadata = getattr(signal, "as_metadata", None)
        if callable(as_metadata):
            return dict(as_metadata())
        return None

    def _param_override(self, key: str, default: Any) -> Any:
        store = self._parameter_store
        if store is None:
            return default
        return store.get("settings", key, default=default)

    @property
    def _portfolio_budget_usdc(self) -> Decimal:
        return self._param_override("portfolio_budget_usdc", self._portfolio_budget_usdc_default)

    @property
    def _kelly_fraction(self) -> Decimal:
        return self._param_override("kelly_fraction", self._kelly_fraction_default)

    @property
    def _kelly_max_position_fraction(self) -> Decimal:
        return self._param_override(
            "kelly_max_position_fraction", self._kelly_max_position_fraction_default
        )

    @property
    def _kelly_min_edge(self) -> Decimal:
        return self._param_override("kelly_min_edge", self._kelly_min_edge_default)

    @property
    def _kelly_min_stake_usdc(self) -> Decimal:
        return self._param_override(
            "kelly_min_stake_usdc", self._kelly_min_stake_usdc_default
        )

    @property
    def _kelly_allow_round_up(self) -> bool:
        return self._param_override(
            "kelly_allow_round_up_to_market_min", self._kelly_allow_round_up_default
        )

    @property
    def _kelly_round_up_max_overbet_ratio(self) -> Decimal:
        return self._param_override(
            "kelly_round_up_max_overbet_ratio",
            self._kelly_round_up_max_overbet_ratio_default,
        )

    @property
    def _order_retry_limit(self) -> int | None:
        return self._param_override("order_retry_limit", self._order_retry_limit_default)

    async def run(self) -> None:
        if self._event_bus is None:
            raise RuntimeError("TradingDecisionWorker requires an EventBus to run")
        self._emit_heartbeat(detail=self._idle_detail("running"))
        while True:
            await self.run_once()

    async def run_once(self) -> "TradingDecisionWorkerResult | None":
        if self._event_bus is None:
            raise RuntimeError("TradingDecisionWorker requires an EventBus to run")
        # 空闲等事件时仍要让 supervisor 区分「卡死」和「无事可做」——超时后只 heartbeat，
        # 不向上抛错，下一轮继续等。idle 时不消耗 CPU；只有真到 timeout 才唤醒一次。
        while True:
            try:
                event = await asyncio.wait_for(
                    self._event_bus.next_trading_event(),
                    timeout=self._idle_heartbeat_seconds,
                )
            except asyncio.TimeoutError:
                self._emit_heartbeat(detail=self._idle_detail("idle"))
                continue
            break
        result = await self.process_event(event)
        self._emit_heartbeat(detail=self._processed_detail(event))
        return result

    async def process_event(self, event: DomainEvent) -> "TradingDecisionWorkerResult | None":
        if is_self_emitted(event):
            return None

        event_name = str(event.event_type)
        snapshot = self._snapshot()
        if event_name in {
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
            DomainEventType.ENTRY_SIGNAL_TRIGGERED.value,
        }:
            return await self._handle_orderbook_snapshot_updated(event, snapshot)

        order_result = coerce_order_result_from_event(event)
        if order_result is not None:
            return await self._handle_order_result(
                source_event=event,
                order_result=order_result,
                snapshot=snapshot,
            )

        if event_name == DomainEventType.POSITION_UPDATED.value:
            return await self._handle_position_updated(event, snapshot)

        return None

    async def _handle_orderbook_snapshot_updated(
        self,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
    ) -> "TradingDecisionWorkerResult | None":
        # 合成事件 dedupe：position_exit_evaluator 每 5s 发合成 event 兜底薄盘
        # 没人推 orderbook 的场景。但同 token 5s 内已有真实 orderbook event 处理过
        # → 该 token 不缺数据，跳过合成 event 避免重复评估。
        is_synthetic = (event.payload or {}).get("synthetic") is True
        if (
            is_synthetic
            and event.token_id
            and event.token_id in self._token_last_real_orderbook_at
        ):
            last_real = self._token_last_real_orderbook_at[event.token_id]
            if (_utc_now() - last_real).total_seconds() < 5.0:
                return None
        if not is_synthetic and event.token_id:
            self._token_last_real_orderbook_at[event.token_id] = _utc_now()
        # 订阅驱动 reprice：每个 orderbook tick 检查该 token 是否有持仓，
        # 有 → 跑 decide_exit 让 _maybe_reprice_stale_sell 用最新 best_bid/fair_value
        # 评估是否 cancel-replace stale SELL。不依赖 60s reconcile 周期。
        # 在 entry 路径之前先 reprice，确保盘口快速反弹时 SELL 立即跟价。
        has_snapshot = snapshot is not None
        has_cid = bool(event.condition_id)
        has_tid = bool(event.token_id)
        if has_snapshot and has_cid and has_tid:
            position = snapshot.get_position(event.condition_id, event.token_id)
            if position is not None and position.shares > Decimal("0"):
                # 订阅驱动 MTM 刷新：从 hot orderbook 拿 best_bid 即时更新
                # position.current_value / cash_pnl，不等 reconcile 60s 周期。
                # 这让 portfolio / admin / 净值显示实时看到当前真实可实现价值。
                # 关键：best_bid=None（冷板凳无买家）→ MTM=$0，强制覆盖
                # data-api 返回的 stale curPrice，避免虚假浮盈（Kalinina case
                # 显示 cv=\$59 但实际 best_bid=None 全损 \$6.24 cost）。
                orderbook = self._trading_decision_service.lookup_orderbook(event.token_id)
                if orderbook is not None:
                    # MTM 用 best_bid（立即可成交价上限），但用 sell_actionable
                    # 守门：NO_BID / CEILING_ONLY / DUST_BID 都视为"无真实可实现
                    # 价值"——CEILING_ONLY 是 MM 在 0.99 接 SELL 的天花板单（不算
                    # 真买家），DUST_BID 是 best 一档 < $5 USDC（穿一笔就没）。
                    if orderbook.sell_actionable and orderbook.best_bid is not None:
                        refreshed = position.with_mark_to_market(orderbook.best_bid)
                    else:
                        refreshed = position.with_mark_to_market(Decimal("0"))
                    self._account_state_store.upsert_position(refreshed)
                    position = refreshed
                logger.info(
                    "tick_reprice_triggered",
                    extra={
                        "condition_id": event.condition_id,
                        "token_id": event.token_id,
                        "position_shares": str(position.shares),
                        "open_sell_shares": str(position.open_sell_shares),
                        "current_value": str(position.current_value) if position.current_value is not None else None,
                    },
                )
                await self._execute_position_exit_if_needed(
                    event=event,
                    snapshot=snapshot,
                    position=position,
                )
            else:
                logger.info(
                    "tick_reprice_skip",
                    extra={
                        "reason": "no_position" if position is None else "zero_shares",
                        "condition_id": event.condition_id,
                        "token_id": event.token_id,
                    },
                )
        else:
            logger.info(
                "tick_reprice_skip",
                extra={
                    "reason": "missing_event_fields",
                    "has_snapshot": has_snapshot,
                    "has_cid": has_cid,
                    "has_tid": has_tid,
                },
            )

        if _entry_gate_closed_for_event(snapshot, event):
            return None
        plan = self._trading_decision_service.build_entry_plan(
            trace_id=event.trace_id,
            condition_id=event.condition_id,
            token_id=event.token_id,
            account_snapshot=snapshot,
            portfolio_budget_usdc=self._portfolio_budget_usdc,
            available_usdc=snapshot_available_usdc(snapshot),
            kelly_fraction=self._kelly_fraction,
            kelly_max_position_fraction=self._kelly_max_position_fraction,
            kelly_min_edge=self._kelly_min_edge,
            kelly_min_stake_usdc=self._kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=self._kelly_allow_round_up,
            kelly_round_up_max_overbet_ratio=self._kelly_round_up_max_overbet_ratio,
            positions=(snapshot.positions if snapshot is not None else tuple(self._positions_provider())),
            open_orders=(
                snapshot.open_orders if snapshot is not None else tuple(self._open_orders_provider())
            ),
            metadata=self._entry_metadata(event, snapshot),
        )
        # AllocationPlan 决策过程结构化落库——payload 含每个候选的 reason /
        # release_reason / target_budget / buy_budget，回答"为什么选这个市场
        # 不选那个"。P3 异步，失败静默；策略层不感知。
        await self._publish_allocation_decision(event=event, plan=plan)
        if plan.market is None or plan.orderbook is None or event.token_id != plan.orderbook.token_id:
            # silent skip 历史上让"candidate ready=True 却无 order_created"难诊断；
            # 这里 INFO 日志带 plan 是否 ready_to_trade + buy_budget，下次排查时直接 grep
            # entry_dispatch_skipped condition_id=<...> 就能区分是 plan 缺数据还是 token 不匹配。
            logger.info(
                "entry_dispatch_skipped",
                extra={
                    "skip_reason": (
                        "plan_market_none" if plan.market is None
                        else "plan_orderbook_none" if plan.orderbook is None
                        else "event_token_mismatch"
                    ),
                    "condition_id": event.condition_id,
                    "event_token_id": event.token_id,
                    "plan_orderbook_token_id": plan.orderbook.token_id if plan.orderbook is not None else None,
                    "plan_ready_to_trade": plan.ready_to_trade,
                    "allocation_buy_budget_usdc": str(plan.allocation.buy_budget_usdc) if plan.allocation is not None else None,
                    "plan_reason": plan.reason or "",
                },
            )
            return None

        state = self._state_for_market(plan.market)
        if state is None:
            self._transition_market(plan.market, MarketLifecycle.WATCHING_ORDERBOOK)
        elif not _state_allows_entry_attempt(
            state,
            plan,
        ):
            logger.info(
                "entry_dispatch_skipped",
                extra={
                    "skip_reason": "lifecycle_not_attemptable",
                    "condition_id": plan.market.condition_id,
                    "event_token_id": event.token_id,
                    "lifecycle_state": state.value,
                    "plan_ready_to_trade": plan.ready_to_trade,
                    "allocation_buy_budget_usdc": str(plan.allocation.buy_budget_usdc) if plan.allocation is not None else None,
                },
            )
            return None
        return await self._execute_entry_plan(event=event, snapshot=snapshot, plan=plan)

    async def _execute_entry_plan(
        self,
        *,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
        plan: EntryPlan,
    ) -> "TradingDecisionWorkerResult":
        positions = snapshot.positions if snapshot is not None else tuple(self._positions_provider())
        open_orders = (
            snapshot.open_orders if snapshot is not None else tuple(self._open_orders_provider())
        )
        if snapshot is not None and not snapshot.allow_new_entries:
            return await self._emit_entry_skip(
                event=event,
                plan=plan,
                trace_id=event.trace_id,
                reason="entry_paused",
                lifecycle=MarketLifecycle.PAUSED,
                extra_payload={"account_snapshot": serialize_snapshot(snapshot)},
                emit_in_tuple=False,
            )

        if not plan.ready_to_trade or plan.intent is None or plan.market is None or plan.orderbook is None:
            return await self._emit_entry_skip(
                event=event,
                plan=plan,
                trace_id=plan.trace_id,
                reason=plan.reason or "entry_not_ready",
                lifecycle=MarketLifecycle.WATCHING_ORDERBOOK,
                extra_payload=None,
                emit_in_tuple=True,
            )

        focus_token_id = plan.intent.token_id
        focus_position = _match_position(positions, plan.market.condition_id, focus_token_id)
        focus_open_orders = _match_open_orders(open_orders, plan.market.condition_id, focus_token_id)
        self._transition_market(plan.market, MarketLifecycle.ENTRY_SUBMITTING)
        review = await self._trading_service.review_intent(
            plan.intent,
            market=plan.market,
            orderbook=plan.orderbook,
            position=focus_position,
            open_orders=focus_open_orders,
            condition_open_orders=_condition_orders(snapshot, plan.market.condition_id),
            condition_positions=_condition_positions(snapshot, plan.market.condition_id),
            allocation_plan=plan.allocation_plan,
            classification_passed=True,
            balance_usdc=snapshot_available_usdc(snapshot),
            allowance_usdc=snapshot_allowance(snapshot),
            bankroll_usdc=_resolve_bankroll_for_review(
                portfolio_budget_usdc=self._portfolio_budget_usdc,
                snapshot=snapshot,
            ),
            kelly_max_position_fraction=self._kelly_max_position_fraction,
            kelly_round_up_max_overbet_ratio=self._kelly_round_up_max_overbet_ratio,
            order_retry_limit=self._order_retry_limit,
            operation=plan.intent.side.value.lower(),
        )
        risk_event = await self._publish(
            DomainEventType.RISK_CHECK_PASSED
            if review.risk_decision is not None and review.risk_decision.passed
            else DomainEventType.RISK_CHECK_FAILED,
            trace_id=plan.trace_id,
            market_slug=plan.market.market_slug,
            condition_id=plan.market.condition_id,
            token_id=plan.intent.token_id,
            reason="risk_decision_unavailable" if review.risk_decision is None else review.risk_decision.reason,
            payload={
                "entry_event_id": event.event_id,
                "origin": TRADING_DECISION_WORKER_ORIGIN,
                "allocation_plan": serialize_allocation_plan(plan),
                "allocation": serialize_allocation(plan),
                "plan_metadata": serialize_plan_metadata(plan),
                "intent": serialize_intent(plan.intent),
                "review": serialize_review(review),
            },
        )
        result = await self._handle_order_result(
            source_event=event,
            order_result=review.order_result,
            snapshot=snapshot,
            execution=review,
            plan=plan,
        )
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=plan,
            review=review,
            emitted_event=risk_event,
            emitted_events=(risk_event, *result.emitted_events),
            follow_up_intents=result.follow_up_intents,
            follow_up_reviews=result.follow_up_reviews,
            state_before=result.state_before,
            state_after=result.state_after,
        )

    async def _handle_order_result(
        self,
        *,
        source_event: DomainEvent,
        order_result: OrderResult | None,
        snapshot: AccountSnapshot | None,
        execution: TradingReviewResult | None = None,
        plan: EntryPlan | None = None,
    ) -> "TradingDecisionWorkerResult":
        return await self._order_result_processor.handle(
            source_event=source_event,
            order_result=order_result,
            snapshot=snapshot,
            execution=execution,
            plan=plan,
        )

    async def _handle_position_updated(
        self,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
    ) -> "TradingDecisionWorkerResult":
        if snapshot is None:
            return TradingDecisionWorkerResult(
                entry_event=event,
                plan=None,
                review=None,
                emitted_event=event,
                state_after=None,
            )
        market = self._market_fromsnapshot_position(snapshot, event.condition_id, event.token_id)
        position = _match_position_for_event(snapshot, event)
        if market is not None:
            self._transition_market(market, MarketLifecycle.POSITION_OPEN)
        if position is None:
            return TradingDecisionWorkerResult(
                entry_event=event,
                plan=None,
                review=None,
                emitted_event=event,
                state_after=self._state_for_market(market),
            )

        exit_result = await self._execute_position_exit_if_needed(
            event=event,
            snapshot=snapshot,
            position=position,
        )
        if exit_result is not None:
            return exit_result
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=None,
            review=None,
            emitted_event=event,
            state_after=self._state_for_market(market),
        )

    async def _execute_position_exit_if_needed(
        self,
        *,
        event: DomainEvent,
        snapshot: AccountSnapshot,
        position: Position,
    ) -> "TradingDecisionWorkerResult | None":
        """在真实持仓更新后让策略决定是否需要退出保护单。

        用户 WS 的成交可能晚于初次下单响应到达。此时热态里已经有持仓，但
        同步 BUY 结果不一定触发跟单 SELL；这里以 position/open_orders 为事实，
        调策略 ``decide_exit``。当前策略默认等待结算会返回 SKIP；如策略返回
        SELL intent，仍走统一风控和执行器。
        """

        market = self._trading_decision_service.resolve_market(
            condition_id=position.condition_id,
            token_id=position.token_id,
        )
        open_orders = snapshot.open_orders_for_market(position.condition_id, position.token_id)
        exit_metadata: dict[str, Any] = {
            "exit_trigger": "position_updated",
            "source_event_id": event.event_id,
            "source_reason": event.reason,
        }
        direction = self._fetch_orderbook_direction(position.token_id)
        if direction is not None:
            exit_metadata["orderbook_direction"] = direction
        decision = self._trading_decision_service.decide_exit(
            ExtensionContext(
                trace_id=event.trace_id,
                strategy_id=self._trading_decision_service.strategy_id,
                market=market,
                token_id=position.token_id,
                orderbook=self._trading_decision_service.lookup_orderbook(position.token_id),
                market_token_views=tuple(
                    MarketTokenView(
                        token_id=outcome.token_id,
                        outcome=outcome.outcome,
                    )
                    for outcome in (() if market is None else market.outcomes)
                ),
                account_snapshot=snapshot,
                position=position,
                open_orders=open_orders,
                metadata=exit_metadata,
            )
        )
        # 缓存决策 metadata 供 /positions/signals admin endpoint 暴露。每次
        # decide_exit 后更新当前 token 的 signals 快照，UI/操盘人可实时看到
        # 5 类投票 + 流动性 tier + math_lock 是否支持 + fair_value 来源。
        if position.token_id and decision.metadata:
            self._token_position_signals[position.token_id] = {
                "condition_id": position.condition_id,
                "token_id": position.token_id,
                "market_slug": (market.market_slug if market is not None else position.market_slug),
                "evaluated_at": _utc_now().isoformat(),
                "decision_action": decision.action.value,
                "decision_reason": decision.reason,
                "decision_price": str(decision.price) if decision.price is not None else None,
                "metadata": {k: v for k, v in decision.metadata.items() if k.startswith("dynamic_exit_")},
            }
        # SELL 直接挂；REPLACE 是 reprice 路径（_maybe_reprice_stale_sell 把 stale
        # $0.99 SELL cancel-replace 到 fair_value × 0.97），不接 REPLACE 会让订阅
        # 触发的 reprice 决策静默丢弃。
        if decision.action not in {ExtensionAction.SELL, ExtensionAction.REPLACE}:
            return None
        intent = self._trading_decision_service.build_intent_from_decision(
            trace_id=event.trace_id,
            condition_id=position.condition_id,
            market_slug=event.market_slug or position.market_slug or (
                None if market is None else market.market_slug
            ),
            default_token_id=position.token_id,
            decision=decision,
        )
        if intent is None:
            return None

        review = await self._execute_managed_intent(intent, snapshot=snapshot)
        emitted = await self._publish(
            DomainEventType.ORDER_SUBMITTED,
            trace_id=intent.trace_id,
            market_slug=intent.market_slug,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            reason="order_result_unavailable" if review.order_result is None else review.order_result.reason,
            payload={
                "phase": "position_exit",
                "source_event_id": event.event_id,
                "decision_metadata": dict(decision.metadata),
                "intent": serialize_intent(intent),
                "review": serialize_review(review),
            },
        )
        self._apply_position_exit_result(
            review,
            intent=intent,
            snapshot=snapshot,
        )
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=None,
            review=review,
            emitted_event=emitted,
            emitted_events=(emitted,),
            follow_up_intents=(intent,),
            follow_up_reviews=(review,),
            state_after=self._state_for_market_by_key(position.condition_id),
        )

    def _apply_position_exit_result(
        self,
        review: TradingReviewResult,
        *,
        intent: ManagedOrderIntent,
        snapshot: AccountSnapshot,
    ) -> AccountSnapshot:
        """把持仓触发的退出单结果投影回热态。"""

        if review.order_result is None:
            return snapshot
        self._transition_from_order_result(review.order_result)
        projector = self._account_projector()
        if projector is not None and isinstance(intent, SellOrderIntent):
            projector.apply_sell_result(
                review.order_result,
                snapshot=snapshot,
                intent=intent,
            )
        if projector is not None:
            projector.apply_result_flags(review.order_result, snapshot=snapshot)
        if self._account_state_store is not None:
            return self._account_state_store.snapshot()
        return snapshot

    async def _emit_entry_skip(
        self,
        *,
        event: DomainEvent,
        plan: EntryPlan,
        trace_id: str,
        reason: str,
        lifecycle: MarketLifecycle,
        extra_payload: Mapping[str, object] | None,
        emit_in_tuple: bool,
    ) -> "TradingDecisionWorkerResult":
        """统一发布入场 SKIP 事件 + 状态转换 + 构造 result。

        ``emit_in_tuple`` 控制是否把 SKIP 事件同时写入 ``emitted_events`` 元组。
        历史上两条入场跳过路径（entry_paused / entry_not_ready）只在该字段、
        ``trace_id`` 来源、``reason`` 和 ``extra_payload`` 上有差别，其余完全相同。
        """

        payload: dict[str, object] = {
            "entry_event_id": event.event_id,
            "origin": TRADING_DECISION_WORKER_ORIGIN,
            "reason": reason,
            "allocation_plan": serialize_allocation_plan(plan),
            "allocation": serialize_allocation(plan),
            "plan_metadata": serialize_plan_metadata(plan),
        }
        if extra_payload:
            payload.update(extra_payload)
        strategy_id = self._trading_decision_service.strategy_id or "unknown"
        key = (strategy_id, reason or "unknown_reason")
        self._skip_reason_histogram[key] = self._skip_reason_histogram.get(key, 0) + 1
        skipped = await self._publish(
            DomainEventType.SKIPPED,
            trace_id=trace_id,
            market_slug=event.market_slug,
            condition_id=event.condition_id,
            token_id=event.token_id,
            reason=reason,
            payload=payload,
            priority=OutboxPriority.P3,
        )
        self._transition_market(plan.market, lifecycle if plan.market else None)
        emitted_events_tuple: tuple[DomainEvent, ...] = (skipped,) if emit_in_tuple else ()
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=plan,
            review=None,
            emitted_event=skipped,
            emitted_events=emitted_events_tuple,
            state_after=self._state_for_market(plan.market),
        )

    async def _publish_allocation_decision(
        self,
        *,
        event: DomainEvent,
        plan: EntryPlan,
    ) -> None:
        """投递 ALLOCATION_DECISION_RECORDED 事件。失败静默，不阻塞主链路。

        emit guard：只有当 ``allocation_plan.allocations`` 实际产生了候选时才
        发——orderbook 每次更新都触发本路径，空 plan 不发避免 audit 表暴涨。
        """

        if self._event_bus is None or plan.allocation_plan is None:
            return
        allocation_plan = plan.allocation_plan
        if not allocation_plan.allocations:
            return
        candidates = []
        for allocation in allocation_plan.allocations:
            candidates.append(
                {
                    "condition_id": allocation.condition_id,
                    "token_id": allocation.token_id,
                    "market_slug": allocation.market_slug,
                    "target_budget_usdc": str(allocation.target_budget_usdc),
                    "buy_budget_usdc": str(allocation.buy_budget_usdc),
                    "current_exposure_usdc": str(allocation.current_exposure_usdc),
                    "released_budget_usdc": str(allocation.released_budget_usdc),
                    "reason": allocation.reason,
                    "release_reason": allocation.release_reason,
                }
            )
        skipped_reasons: dict[str, int] = {}
        for allocation in allocation_plan.allocations:
            r = allocation.release_reason or allocation.reason or ""
            if r:
                skipped_reasons[r] = skipped_reasons.get(r, 0) + 1
        selected = [
            allocation.condition_id
            for allocation in allocation_plan.allocations
            if allocation.buy_budget_usdc > 0
        ]
        # dedup hash：稳定字段集合
        #   - 每条 allocation 的 (cid, token, reason, release_reason, buy_budget>0)
        #     —— 精确 buy_budget_usdc 数值会随 bankroll/orderbook 每 tick 抖动，把它
        #     纳入 hash 会让 dedup 永远不命中；只看是否拿到预算这个布尔位。
        #   - 选中市场列表（排序）
        #   - plan 顶层 reason
        # 任何"决策本质"变化都会命中；纯数值抖动会被吸收。
        candidate_state = sorted(
            (
                allocation.condition_id,
                allocation.token_id,
                allocation.reason,
                allocation.release_reason,
                allocation.buy_budget_usdc > 0,
            )
            for allocation in allocation_plan.allocations
        )
        hash_payload = json.dumps(
            {
                "candidates": candidate_state,
                "selected": sorted(selected),
                "plan_reason": allocation_plan.reason or "",
            },
            sort_keys=True,
            default=str,
        )
        state_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
        dedup_key = (event.condition_id, event.token_id)
        previous_hash = self._last_allocation_state_hash.get(dedup_key)
        if previous_hash == state_hash:
            self._last_allocation_state_hash.move_to_end(dedup_key)
            self._suppressed_allocation_emits += 1
            return
        self._last_allocation_state_hash[dedup_key] = state_hash
        self._last_allocation_state_hash.move_to_end(dedup_key)
        while len(self._last_allocation_state_hash) > _ALLOCATION_DEDUPE_CAPACITY:
            self._last_allocation_state_hash.popitem(last=False)
        try:
            await self._event_bus.publish(
                OutboxPriority.P3,
                DomainEvent(
                    trace_id=plan.trace_id or event.trace_id,
                    event_type=DomainEventType.ALLOCATION_DECISION_RECORDED,
                    event_id=uuid4().hex,
                    market_slug=plan.market.market_slug if plan.market is not None else None,
                    condition_id=event.condition_id,
                    token_id=event.token_id,
                    reason=allocation_plan.reason or "",
                    payload={
                        "candidates": candidates,
                        "selected_condition_ids": selected,
                        "skipped_reasons": skipped_reasons,
                        "total_budget_usdc": str(allocation_plan.total_budget_usdc),
                        "buy_budget_usdc": str(allocation_plan.allocated_budget_usdc),
                        "allocator": TRADING_DECISION_WORKER_ORIGIN,
                    },
                ),
            )
        except Exception:
            return

    async def _publish(
        self,
        event_type: DomainEventType,
        *,
        trace_id: str,
        market_slug: str | None,
        condition_id: str | None,
        token_id: str | None,
        reason: str = "",
        payload: Mapping[str, object] | None = None,
        priority: OutboxPriority = OutboxPriority.P0,
    ) -> DomainEvent:
        payload_dict: dict[str, object] = {"origin": TRADING_DECISION_WORKER_ORIGIN}
        if payload is not None:
            payload_dict.update(payload)
        event = DomainEvent(
            trace_id=trace_id,
            event_type=event_type,
            event_id=uuid4().hex,
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
            reason=reason,
            created_at=_utc_now(),
            payload=payload_dict,
        )
        if self._event_bus is not None:
            await self._event_bus.publish(priority, event)
        return event

    def _entry_metadata(
        self,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
    ) -> dict[str, object]:
        """构造入场策略 metadata。

        Worker 只合并事件事实和外部 provider 提供的补充事实，不解释具体策略字段。
        """

        metadata: dict[str, object] = dict(event.payload)
        if self._entry_metadata_provider is None:
            return metadata
        try:
            extra_metadata = self._entry_metadata_provider(event, snapshot)
        except Exception as exc:
            metadata["entry_metadata_provider_error"] = str(exc)
            return metadata
        if extra_metadata:
            metadata.update(dict(extra_metadata))
        return metadata

    def bind_heartbeat(self, heartbeat: HeartbeatCallback | None) -> None:
        """Runtime 装配 supervisor.heartbeat_worker 的轻量适配点；测试可注入假回调。"""

        self._heartbeat = heartbeat

    @property
    def suppressed_allocation_emits(self) -> int:
        """已被 dedup 抑制的 allocation_decision_recorded 事件数，供 observability/admin 观察。"""

        return self._suppressed_allocation_emits

    def _emit_heartbeat(self, *, detail: str) -> None:
        """对外发心跳——supervisor 看不到心跳就视为 worker 卡死。

        任何回调异常都吞掉：观测路径绝不能反向阻塞 P0 主链路（CLAUDE.md §7）。
        """

        callback = self._heartbeat
        if callback is None:
            return
        try:
            callback(detail=detail)
        except Exception:
            # 故意吞掉异常：心跳是观测副作用，不能影响交易决策路径。
            return

    def _idle_detail(self, prefix: str) -> str:
        return f"{prefix} qd={self._trading_queue_depth_safe()}"

    def _processed_detail(self, event: DomainEvent) -> str:
        return f"processed event_type={event.event_type} qd={self._trading_queue_depth_safe()}"

    def _trading_queue_depth_safe(self) -> int:
        bus = self._event_bus
        if bus is None:
            return 0
        try:
            return int(bus.trading_queue_depth())
        except Exception:
            return 0

    def _snapshot(self) -> AccountSnapshot | None:
        if self._account_state_store is not None:
            return self._account_state_store.snapshot()
        return None

    def _build_positions_provider(self) -> PositionsProvider:
        if self._account_state_store is None:
            return lambda: ()
        account_state_store = self._account_state_store
        return lambda: account_state_store.snapshot().positions

    def _build_open_orders_provider(self) -> OpenOrdersProvider:
        if self._account_state_store is None:
            return lambda: ()
        account_state_store = self._account_state_store
        return lambda: account_state_store.snapshot().open_orders

    def _transition_market(self, market: Market | None, lifecycle: MarketLifecycle | None) -> None:
        if market is None or lifecycle is None:
            return
        self._market_lifecycle[market.condition_id] = lifecycle
        self._record_lifecycle(market.condition_id, lifecycle)

    def _record_lifecycle(self, condition_id: str, lifecycle: MarketLifecycle) -> None:
        if condition_id not in self._lifecycle_timeline:
            if len(self._lifecycle_timeline) >= _LIFECYCLE_MARKET_CAP:
                self._lifecycle_timeline.popitem(last=False)
            self._lifecycle_timeline[condition_id] = []
        else:
            self._lifecycle_timeline.move_to_end(condition_id)
        history = self._lifecycle_timeline[condition_id]
        history.append((lifecycle, _utc_now()))
        if len(history) > _LIFECYCLE_HISTORY_PER_MARKET:
            del history[: len(history) - _LIFECYCLE_HISTORY_PER_MARKET]

    def _transition_market_by_result(self, order_result: OrderResult, lifecycle: MarketLifecycle) -> None:
        market = market_from_result(order_result)
        self._transition_market(market, lifecycle)

    def _transition_from_order_result(self, order_result: OrderResult) -> None:
        if order_result.side is None:
            return
        side = str(order_result.side).upper()
        if side == "BUY":
            if order_result.status in {OrderResultStatus.FULL_FILL, OrderResultStatus.PARTIAL_FILL}:
                self._transition_market_by_result(order_result, MarketLifecycle.POSITION_OPEN)
            elif order_result.status == OrderResultStatus.LIVE:
                # 买单挂单未成交 → 暂停该市场直到 reconciler 处理。与 _pause_market 保持
                # 同步，确保 AccountStateStore.is_market_paused 返回 True，让 admin 可见。
                self._pause_market(order_result.condition_id, reason="resting_buy_order")
            elif order_result.status == OrderResultStatus.NO_FILL:
                self._transition_market_by_result(order_result, MarketLifecycle.ENTRY_READY)
            elif order_result.status in {OrderResultStatus.REJECTED, OrderResultStatus.FAILED, OrderResultStatus.UNKNOWN_TIMEOUT}:
                self._transition_market_by_result(order_result, MarketLifecycle.ENTRY_REJECTED)
        elif side == "SELL":
            if order_result.status in {OrderResultStatus.LIVE, OrderResultStatus.PARTIAL_FILL}:
                self._transition_market_by_result(order_result, MarketLifecycle.FOLLOW_UP_ORDER_OPEN)
            elif order_result.status == OrderResultStatus.FULL_FILL:
                self._transition_market_by_result(order_result, MarketLifecycle.POSITION_OPEN)

    def _pause_market(self, condition_id: str | None, *, reason: str) -> None:
        if condition_id is None:
            return
        self._market_lifecycle[condition_id] = MarketLifecycle.PAUSED
        self._record_lifecycle(condition_id, MarketLifecycle.PAUSED)
        if self._account_state_store is not None:
            self._account_state_store.pause_market(
                condition_id,
                reason=reason,
                source=MarketPauseSource.RISK,
            )

    def _state_for_market(self, market: Market | None) -> MarketLifecycle | None:
        if market is None:
            return None
        return self._market_lifecycle.get(market.condition_id)

    def _state_for_market_by_key(self, condition_id: str | None) -> MarketLifecycle | None:
        if condition_id is None:
            return None
        return self._market_lifecycle.get(condition_id)

    @property
    def skip_reason_histogram(self) -> dict[tuple[str, str], int]:
        """P3.5: (strategy_id, reason) → count。只读快照，admin 查询用。"""
        return dict(self._skip_reason_histogram)

    @property
    def lifecycle_timeline(self) -> dict[str, list[tuple[MarketLifecycle, datetime]]]:
        """P3.6: condition_id → [(lifecycle, utc_timestamp), …]。只读快照，admin 查询用。"""
        return {cid: list(entries) for cid, entries in self._lifecycle_timeline.items()}

    def _market_fromsnapshot_position(
        self,
        snapshot: AccountSnapshot,
        condition_id: str | None,
        token_id: str | None,
    ) -> Market | None:
        if condition_id is None or token_id is None:
            return None
        position = snapshot.get_position(condition_id, token_id)
        if position is None:
            return None
        market = self._trading_decision_service.resolve_market(condition_id=condition_id, token_id=token_id)
        return market

    async def _execute_managed_intent(
        self,
        intent: ManagedOrderIntent,
        *,
        snapshot: AccountSnapshot | None,
    ) -> TradingReviewResult:
        market = self._trading_decision_service.resolve_market(
            condition_id=intent.condition_id,
            token_id=intent.token_id,
        )
        if isinstance(intent, CancelOrderIntent):
            return await self._trading_service.cancel(intent)
        if isinstance(intent, ReplaceOrderIntent):
            result = await self._trading_service.replace(intent)
            logger.info(
                "replace_intent_result",
                extra={
                    "order_id": intent.order_id,
                    "new_price": str(intent.new_price),
                    "size_shares": str(intent.size_shares),
                    "submitted": result.submitted,
                    "submission_error": result.submission_error,
                    "order_result_status": (
                        result.order_result.status.value if result.order_result is not None and result.order_result.status is not None else None
                    ),
                    "order_result_reason": (
                        result.order_result.reason if result.order_result is not None else None
                    ),
                },
            )
            return result
        return await self._trading_service.review_intent(
            intent,
            market=market,
            orderbook=self._trading_decision_service.lookup_orderbook(intent.token_id),
            position=snapshot_position(snapshot, intent.condition_id, intent.token_id),
            open_orders=(
                snapshot.open_orders_for_market(intent.condition_id, intent.token_id)
                if snapshot is not None
                else ()
            ),
            condition_open_orders=_condition_orders(snapshot, intent.condition_id),
            condition_positions=_condition_positions(snapshot, intent.condition_id),
            classification_passed=True,
            balance_usdc=snapshot_available_usdc(snapshot),
            allowance_usdc=snapshot_allowance(snapshot),
            bankroll_usdc=_resolve_bankroll_for_review(
                portfolio_budget_usdc=self._portfolio_budget_usdc,
                snapshot=snapshot,
            ),
            kelly_max_position_fraction=self._kelly_max_position_fraction,
            kelly_round_up_max_overbet_ratio=self._kelly_round_up_max_overbet_ratio,
            order_retry_limit=self._order_retry_limit,
            operation=intent.side.value.lower(),
        )

    def _account_projector(self) -> AccountStateProjector | None:
        if self._account_state_store is None:
            return None
        return AccountStateProjector(
            self._account_state_store,
            strategy_id=self._trading_decision_service.strategy_id,
        )


def _match_position(
    positions: tuple[Position, ...],
    condition_id: str,
    token_id: str,
) -> Position | None:
    for position in positions:
        if position.condition_id == condition_id and position.token_id == token_id:
            return position
    return None


def _plan_allows_position_increase(plan: EntryPlan) -> bool:
    """判断计划是否是策略显式标记的受控加仓。

    依据策略在决策对象上声明的 intent_tags（含 ``"scale_in"``）+ intent
    自身的 ``allow_open_exit_overlap`` 双重标记，避免读策略私有 metadata 字符串。
    """

    intent = plan.intent
    if intent is None or not intent.allow_open_exit_overlap:
        return False
    tags = intent.intent_tags or frozenset()
    return "scale_in" in tags


def _state_allows_position_increase(state: MarketLifecycle, plan: EntryPlan) -> bool:
    """只有持仓相关生命周期允许策略受控加仓继续走主链路。"""

    return state in POSITION_INCREASE_LIFECYCLES and _plan_allows_position_increase(plan)


def _state_allows_entry_attempt(state: MarketLifecycle, plan: EntryPlan) -> bool:
    """判断当前生命周期是否允许继续处理新的入场信号。

    ENTRY_READY 表示上一轮没有形成外部持仓副作用，例如 FAK no-fill
    或风控层可重试拒绝；后续盘口变好时应继续评估。已有持仓或退出单时，
    仍只允许策略显式标记的受控加仓进入 BUY 主链路。
    """

    return state in ENTRY_ATTEMPT_LIFECYCLES or _state_allows_position_increase(state, plan)


def _match_open_orders(
    open_orders: tuple[Order, ...],
    condition_id: str,
    token_id: str,
) -> tuple[Order, ...]:
    return tuple(
        order
        for order in open_orders
        if order.condition_id == condition_id and order.token_id == token_id
    )


def _condition_orders(
    snapshot: AccountSnapshot | None,
    condition_id: str | None,
) -> tuple[Order, ...]:
    """同 condition_id 的全部 open orders（跨 token），用于 NEG_RISK 互斥检测。"""

    if snapshot is None or not condition_id:
        return ()
    return tuple(order for order in snapshot.open_orders if order.condition_id == condition_id)


def _condition_positions(
    snapshot: AccountSnapshot | None,
    condition_id: str | None,
) -> tuple[Position, ...]:
    """同 condition_id 的全部持仓（跨 token），用于 NEG_RISK 互斥检测。"""

    if snapshot is None or not condition_id:
        return ()
    return tuple(p for p in snapshot.positions if p.condition_id == condition_id)


def _resolve_bankroll_for_review(
    *,
    portfolio_budget_usdc: Decimal,
    snapshot: AccountSnapshot | None,
) -> Decimal:
    """与 EntryPlanner 内 ``_resolve_bankroll`` 一致的口径，避免 RiskManager 与
    EntryPlanner 用不同的 bankroll 数。"""

    if snapshot is None:
        bankroll = portfolio_budget_usdc
    else:
        bankroll = min(snapshot.available_usdc, portfolio_budget_usdc)
    if bankroll < Decimal("0"):
        return Decimal("0")
    return bankroll


def _match_position_for_event(
    snapshot: AccountSnapshot,
    event: DomainEvent,
) -> Position | None:
    """从当前热态中找到 position update 对应的持仓。"""

    if event.condition_id is not None and event.token_id is not None:
        return snapshot.get_position(event.condition_id, event.token_id)
    payload_position = event.payload.get("position")
    if isinstance(payload_position, Mapping):
        condition_id = payload_position.get("condition_id")
        token_id = payload_position.get("token_id")
        if condition_id is not None and token_id is not None:
            return snapshot.get_position(str(condition_id), str(token_id))
    payload_positions = event.payload.get("positions")
    if isinstance(payload_positions, (list, tuple)):
        for payload_item in payload_positions:
            if not isinstance(payload_item, Mapping):
                continue
            condition_id = payload_item.get("condition_id")
            token_id = payload_item.get("token_id")
            if condition_id is None or token_id is None:
                continue
            position = snapshot.get_position(str(condition_id), str(token_id))
            if position is not None:
                return position
    return None
