"""Shadow run：按时间序消费 ``ShadowEvent`` 流，模拟一段时间窗内的决策与撮合。

工作流：
1. 调用方传入：策略 hooks、starting balance、事件流、ledger
2. shadow_runner 内部构造独立的影子 runtime（registry / market_ws / metadata
   store / account_store / paper executor / worker）
3. 顺序消费事件：每事件更新影子 runtime → 构造 DomainEvent → 触发 worker
4. 输出 ``ShadowSessionReport``：决策序列 + 累计 ledger 状态 + fees 总额 +
   每事件成交快照

不与现有 ``replay_harness.py`` / ``trade_replay.py`` 重复：那些是离线决策
diff / 成交聚合；shadow_runner 是"事件流时序推进 + 撮合 + ledger 累计"，
新职责。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

from polymarket_trader.app.decision_context_builder import DecisionContextBuilder
from polymarket_trader.app.order_gateway import OrderGateway
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.infra.outbox.local_queue import LocalOutbox
from polymarket_trader.infra.polymarket.order_executor import PolymarketOrderExecutor
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.market_metadata import MarketMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.market_tick import MarketTickWorker

from .client import PaperSubmitOnlyOrderClient
from .event_stream import EventStreamSource, ShadowEvent
from .state import PaperVirtualLedger
from .virtual_clock import EventTimestampClock, VirtualClock

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ShadowDecisionFrame:
    """单个事件处理后的快照：决策摘要 + 当时 ledger 状态 + 错误。"""

    event: ShadowEvent
    plan_ready: bool | None
    plan_action: str | None
    plan_reason: str | None
    entry_order_status: str | None
    entry_filled_shares: str | None
    entry_spent_usdc: str | None
    follow_up_count: int
    available_usdc_after: str
    fees_accrued_after: str
    position_after: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ShadowSessionReport:
    """Shadow run 的全量输出。

    ``entry_fills`` 只统计真实成交（FULL_FILL / PARTIAL_FILL）；``resting_orders_placed``
    统计 LIVE 状态（GTC 挂单成功）；``timeouts`` 统计被超时跳过的事件。
    """

    frames: tuple[ShadowDecisionFrame, ...]
    final_available_usdc: str
    final_fees_accrued_usdc: str
    final_positions: Mapping[str, str]
    events_processed: int
    entry_fills: int
    resting_orders_placed: int
    timeouts: int


@dataclass(slots=True)
class _ShadowMarketWs:
    """影子市场快照 store；每事件按 token_id 覆盖。"""

    snapshots: dict[str, OrderbookSnapshot] = field(default_factory=dict)

    def snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        return self.snapshots.get(token_id)

    def update(self, token_id: str, snapshot: OrderbookSnapshot) -> None:
        self.snapshots[token_id] = snapshot


async def run_shadow_session(
    stream: EventStreamSource,
    *,
    strategy: Any,
    ledger: PaperVirtualLedger,
    starting_balance_usdc: Decimal,
    portfolio_budget_usdc: Decimal,
    kelly_fraction: Decimal = Decimal("0.25"),
    kelly_max_position_fraction: Decimal = Decimal("0.10"),
    kelly_min_edge: Decimal = Decimal("0.02"),
    kelly_min_stake_usdc: Decimal = Decimal("1"),
    kelly_allow_round_up_to_market_min: bool = True,
    kelly_round_up_max_overbet_ratio: Decimal = Decimal("1"),
    virtual_clock: VirtualClock | None = None,
    real_sign_client: Any = None,
    process_event_timeout_seconds: float = 5.0,
) -> ShadowSessionReport:
    """顺序消费事件流，返回完整 shadow 报告。

    ``strategy`` 由调用方提供（生产场景从 TradingWorkflow 取，测试场景可
    构造任意 hooks）。``ledger`` 用于累计成交账本，调用方可读取。``virtual_clock``
    若为 None 则使用 EventTimestampClock 跟随事件时间戳。
    """

    registry = MarketRegistry()
    market_ws = _ShadowMarketWs()
    metadata_store = MarketMetadataStore()
    account_store = AccountStateStore()
    account_store.update_balances(balance_usdc=starting_balance_usdc, allowance_usdc=starting_balance_usdc)
    account_store.mark_user_ws_connected(True)
    if ledger.available_usdc == Decimal("0"):
        ledger.fund(starting_balance_usdc)
    clock = virtual_clock or EventTimestampClock()

    decision_service = DecisionContextBuilder(
        strategy=strategy,
        registry=registry,
        orderbook_reader=market_ws.snapshot,
    )
    paper_client = PaperSubmitOnlyOrderClient(
        real_sign_client=real_sign_client,
        market_lookup=registry.get_by_token_id,
        orderbook_lookup=market_ws.snapshot,
        ledger=ledger,
    )
    outbox = LocalOutbox(max_size=200)
    executor = PolymarketOrderExecutor(
        client=paper_client,
        outbox=outbox,
        sign_timeout_ms=1000,
        submit_timeout_ms=1000,
        critical_lock_timeout_ms=20,
    )
    def _entry_metadata_for_event(event: DomainEvent, _snapshot: Any) -> Mapping[str, Any] | None:
        market = None
        if event.condition_id is not None:
            market = registry.get_by_condition_id(event.condition_id)
        if market is None and event.token_id is not None:
            market = registry.get_by_token_id(event.token_id)
        if market is None and event.market_slug is not None:
            market = registry.get_by_slug(event.market_slug)
        return metadata_store.metadata_for_event(event, market=market)

    try:
        worker = MarketTickWorker(
            decision_context_builder=decision_service,
            order_gateway=OrderGateway(executor=executor),
            account_state_store=account_store,
            portfolio_budget_usdc=portfolio_budget_usdc,
            kelly_fraction=kelly_fraction,
            kelly_max_position_fraction=kelly_max_position_fraction,
            kelly_min_edge=kelly_min_edge,
            kelly_min_stake_usdc=kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=kelly_allow_round_up_to_market_min,
            kelly_round_up_max_overbet_ratio=kelly_round_up_max_overbet_ratio,
            entry_metadata_provider=_entry_metadata_for_event,
        )

        frames: list[ShadowDecisionFrame] = []
        entry_fills = 0
        resting_orders_placed = 0
        timeouts = 0
        for event in stream:
            if isinstance(clock, EventTimestampClock):
                clock.set(event.observed_at)
            _apply_event_to_runtime(
                event,
                registry=registry,
                market_ws=market_ws,
                metadata_store=metadata_store,
            )
            # 刷新 reconcile 时间，让账户状态门保持开放（生产由 reconciler 周期维护，
            # 影子 runtime 没有 reconciler，必须随事件推进）
            account_store.mark_reconciled(event.observed_at)
            # 让 worker 看到与 ledger 同步的可用余额 + 持仓，避免反复消耗起始预算、
            # 也让 exit overlay / scale-in 能基于真实持仓触发。
            account_store.update_balances(
                balance_usdc=ledger.available_usdc,
                allowance_usdc=ledger.available_usdc,
            )
            account_store.replace_positions(_ledger_positions_for_account(ledger, registry))
            domain_event = _build_domain_event(event)
            error: str | None = None
            try:
                result = await asyncio.wait_for(
                    worker.process_event(domain_event),
                    timeout=process_event_timeout_seconds,
                )
            except asyncio.TimeoutError:
                result = None
                error = f"timeout_after_{process_event_timeout_seconds:.1f}s"
                timeouts += 1
            except Exception as exc:  # noqa: BLE001 - 影子 runtime 隔离任何 worker 异常并记录原因
                result = None
                error = f"{type(exc).__name__}: {exc}"
            if result is not None and result.review is not None and result.review.order_result is not None:
                status = result.review.order_result.status.value
                if status in {"full_fill", "partial_fill"}:
                    entry_fills += 1
                elif status == "live":
                    resting_orders_placed += 1
            frames.append(_capture_frame(event, result, ledger, error=error))
        return ShadowSessionReport(
            frames=tuple(frames),
            final_available_usdc=str(ledger.available_usdc),
            final_fees_accrued_usdc=str(ledger.fees_accrued_usdc),
            final_positions={token: str(shares) for token, shares in ledger.positions.items()},
            events_processed=len(frames),
            entry_fills=entry_fills,
            resting_orders_placed=resting_orders_placed,
            timeouts=timeouts,
        )
    finally:
        executor.close()


def _ledger_positions_for_account(
    ledger: PaperVirtualLedger,
    registry: MarketRegistry,
) -> tuple[Position, ...]:
    """把 ledger 持仓投射成 ``AccountStateStore`` 期望的 ``Position`` 元组。

    用 registry 反查 condition_id / market_slug；shares 与 cost_usdc 都从 ledger 取，
    其他字段保持默认（未绑定真实 Polymarket Data API）。worker 的 exit overlay 主要
    依赖 ``shares`` 和 ``cost_usdc``。
    """

    positions: list[Position] = []
    for token_id, shares in ledger.positions.items():
        if shares <= _ZERO:
            continue
        market = registry.get_by_token_id(token_id)
        condition_id = market.condition_id if market is not None else ""
        market_slug = market.market_slug if market is not None else None
        cost = ledger.cost_for(token_id)
        avg_price = (cost / shares) if shares > _ZERO else None
        positions.append(
            Position(
                condition_id=condition_id,
                token_id=token_id,
                shares=shares,
                cost_usdc=cost,
                market_slug=market_slug,
                confirmed_shares=shares,
                confirmation_status="paper_simulated",
                avg_price=avg_price,
            )
        )
    return tuple(positions)


def _apply_event_to_runtime(
    event: ShadowEvent,
    *,
    registry: MarketRegistry,
    market_ws: _ShadowMarketWs,
    metadata_store: MarketMetadataStore,
) -> None:
    """把事件 snapshot 落到影子 runtime stores 中。

    ``ShadowEvent.live_metadata`` 契约：永远是**内层 game state**（含 league /
    home_score / away_score / period / status / observed_at 等字段），由
    shadow_runner 统一包装为 ``{"live_game": ...}`` 喂给 market_metadata_store。
    调用方不要预先包装，避免双重嵌套。
    """

    registry.upsert(event.market)
    market_ws.update(event.token_id, event.orderbook)
    if event.live_metadata:
        metadata_store.upsert(
            condition_id=event.condition_id,
            source=str(event.live_metadata.get("source") or "shadow"),
            metadata={"live_game": dict(event.live_metadata)},
        )


def _build_domain_event(event: ShadowEvent) -> DomainEvent:
    return DomainEvent(
        trace_id=event.trace_id or f"shadow-{uuid4().hex[:8]}",
        event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
        event_id=f"shadow-evt-{uuid4().hex[:8]}",
        market_slug=event.market_slug,
        event_slug=event.market.event_slug,
        condition_id=event.condition_id,
        token_id=event.token_id,
        payload={"shadow_event_observed_at": event.observed_at.isoformat()},
    )


def _capture_frame(
    event: ShadowEvent,
    result: Any | None,
    ledger: PaperVirtualLedger,
    *,
    error: str | None = None,
) -> ShadowDecisionFrame:
    plan = None if result is None else result.plan
    review = None if result is None else result.review
    entry_order = None if review is None else review.order_result
    follow_ups = () if result is None else result.follow_up_intents or ()
    summary = None if plan is None else plan.summary
    return ShadowDecisionFrame(
        event=event,
        plan_ready=None if plan is None else plan.ready_to_trade,
        plan_action=None if summary is None else (summary.action or None),
        plan_reason=None if plan is None else (plan.reason or (summary.reason if summary else None)),
        entry_order_status=None if entry_order is None else entry_order.status.value,
        entry_filled_shares=None if entry_order is None else str(entry_order.matched_shares),
        entry_spent_usdc=None if entry_order is None else str(entry_order.spent_usdc),
        follow_up_count=len(follow_ups),
        available_usdc_after=str(ledger.available_usdc),
        fees_accrued_after=str(ledger.fees_accrued_usdc),
        position_after=str(ledger.position_for(event.token_id)),
        error=error,
    )
