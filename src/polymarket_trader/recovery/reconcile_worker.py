from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from polymarket_trader.observability.cpu_track import cpu_track
from polymarket_trader.recovery.reconcile_service import (
    ReconcileAction,
    ReconcilePlan,
    ReconcileService,
)
from polymarket_trader.app.market_tracking_policy import market_unsubscribe_prune_reason
from polymarket_trader.pipeline.execution.order_gateway import OrderGateway
from polymarket_trader.domain.account import AccountSnapshot, MarketPauseSource
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.runtime.lifecycle_bus import LifecycleEvent
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.gamma_snapshot_store import GammaMarketSnapshotStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.lifecycle_bus import LifecyclePublisher
from polymarket_trader.runtime.registry import MarketRegistry, MarketRegistrySnapshot
from polymarket_trader.serialization import jsonable
from polymarket_trader.pipeline.ingest.orderbook_ws import MarketWsWorker
from .action_applier import ReconcileActionApplier
from .authority_refresher import (
    AuthoritativeRefreshFailure,
    AuthoritativeRefreshSummary,
    DataAuthorityClient,
    MarketAuthorityClient,
    OrderAuthorityClient,
    ReconcileAuthorityRefresher,
    TradingAuthorityClient,
)

RegistrySnapshotProvider = Callable[[], MarketRegistrySnapshot]
AccountSnapshotProvider = Callable[[], AccountSnapshot]


logger = logging.getLogger(__name__)

# 每扫描这么多 market_plan 让一次 event loop——配合 await asyncio.sleep(0)
# 把 P0 trading 事件的处理时机让出来。20 来自经验：100 个 market 的扫描在
# 笔记本上单次 ~30ms，每 20 个让一次相当于 6ms 一次切换，对 reconcile 总
# 时长几乎无影响，但能保证 P0 不会等超过 ~10ms。
_RECONCILE_YIELD_EVERY = 20


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ReconcileWorkerResult:
    trace_id: str
    plan: ReconcilePlan
    applied_actions: tuple[ReconcileAction, ...]
    failed_actions: tuple[tuple[ReconcileAction, str], ...]


@dataclass(frozen=True, slots=True)
class ReconcileWorkerResultSummary:
    trace_id: str
    trigger_event_type: str | None
    started_at: datetime
    completed_at: datetime
    market_count: int
    diff_count: int
    action_count: int
    applied_action_count: int
    failed_action_count: int
    refresh_failures: tuple[AuthoritativeRefreshFailure, ...]


@dataclass(frozen=True, slots=True)
class ReconcileWorkerStatus:
    running: bool
    last_trace_id: str | None
    last_trigger_event_type: str | None
    last_started_at: datetime | None
    last_completed_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    last_refresh_summary: AuthoritativeRefreshSummary | None
    last_result: ReconcileWorkerResultSummary | None
    recent_results: tuple[ReconcileWorkerResultSummary, ...]


class ReconcileWorker:
    priority = "P2"

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        reconcile_service: ReconcileService | None = None,
        registry_snapshot_provider: RegistrySnapshotProvider | None = None,
        account_snapshot_provider: AccountSnapshotProvider | None = None,
        account_state_store: AccountStateStore | None = None,
        order_gateway: OrderGateway | None = None,
        registry: MarketRegistry | None = None,
        market_ws_worker: MarketWsWorker | None = None,
        gamma_client: MarketAuthorityClient | None = None,
        clob_client: OrderAuthorityClient | None = None,
        data_client: DataAuthorityClient | None = None,
        trading_client: TradingAuthorityClient | None = None,
        lifecycle_bus: "LifecyclePublisher | None" = None,
        paper_mode: bool = False,
        gamma_snapshot_store: GammaMarketSnapshotStore | None = None,
        refresh_account_inline: bool = True,
    ) -> None:
        self._event_bus = event_bus
        self._lifecycle_bus = lifecycle_bus
        if reconcile_service is None:
            raise ValueError("reconcile_service is required")
        self._reconcile_service = reconcile_service
        self._registry_snapshot_provider = registry_snapshot_provider
        self._account_snapshot_provider = account_snapshot_provider
        self._account_state_store = account_state_store
        self._order_gateway = order_gateway
        self._registry = registry
        self._market_ws_worker = market_ws_worker
        self._authority_refresher = ReconcileAuthorityRefresher(
            registry_snapshot_provider=registry_snapshot_provider,
            account_state_store=account_state_store,
            registry=registry,
            market_ws_worker=market_ws_worker,
            gamma_client=gamma_client,
            clob_client=clob_client,
            data_client=data_client,
            trading_client=trading_client,
            paper_mode=paper_mode,
            gamma_snapshot_store=gamma_snapshot_store,
            refresh_account_inline=refresh_account_inline,
        )
        self._running = False
        self._last_trace_id: str | None = None
        self._last_trigger_event_type: str | None = None
        self._last_started_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._last_refresh_summary: AuthoritativeRefreshSummary | None = None
        self._last_result: ReconcileWorkerResultSummary | None = None
        self._recent_results: deque[ReconcileWorkerResultSummary] = deque(maxlen=8)
        self._action_applier = ReconcileActionApplier(
            order_gateway=order_gateway,
            account_state_store=account_state_store,
        )
        # audit 节流:reconcile 每 20s 跑 200+ markets,逐条 publish
        # TRADING_PAUSED/RECONCILE_APPLIED/RECONCILE_DIFF_DETECTED 1h 累计 250k+
        # 条 audit(占 audit_events 55%).策略:
        # - TRADING_PAUSED_FOR_MARKET: 状态去重(only new pause,不重发已 paused)
        # - RECONCILE_APPLIED: 60s/condition_id 最小间隔
        # - RECONCILE_DIFF_DETECTED: 保留(真实操作历史)但 30s 兜底
        # audit dedupe + 节流计时器全部迁移到 registry.companion(cid),
        # market prune 时 companion 自动消失,无需手动维护"按 cid 索引"的 dict.
        # reconcile 20s 周期 × 200 markets/轮 → 60s throttle 只能 cover 3 轮,实测仍 10/s.
        # 提到 300s applied / 180s diff:9 轮 reconcile 周期才 emit 1 次,可见率仍足够复盘.
        self._reconcile_applied_min_interval_s: float = 300.0
        self._reconcile_diff_min_interval_s: float = 180.0

    async def run(self) -> None:
        if self._event_bus is None:
            raise RuntimeError("ReconcileWorker requires an EventBus to run")
        while True:
            await self.run_once()

    @cpu_track("reconcile")
    async def run_once(self) -> ReconcileWorkerResult:
        trigger = None
        if self._event_bus is not None:
            trigger = await self._event_bus.next_maintenance_event()
        trace_id = (trigger.trace_id if trigger is not None else None) or uuid4().hex
        started_at = _utc_now()
        self._running = True
        self._last_started_at = started_at
        self._last_trace_id = trace_id
        self._last_trigger_event_type = None if trigger is None else str(trigger.event_type)
        try:
            result = await self.reconcile_once(
                trace_id=trace_id,
                trigger_event=trigger,
                refresh_market_authority=_event_requests_market_authority_refresh(trigger),
            )
        except Exception as exc:
            self._last_error = str(exc)
            self._last_completed_at = _utc_now()
            raise
        else:
            completed_at = _utc_now()
            self._last_completed_at = completed_at
            self._last_success_at = completed_at
            self._last_error = None
            summary = ReconcileWorkerResultSummary(
                trace_id=result.trace_id,
                trigger_event_type=self._last_trigger_event_type,
                started_at=started_at,
                completed_at=completed_at,
                market_count=len(result.plan.market_plans),
                diff_count=result.plan.diff_count,
                action_count=sum(len(plan.actions) for plan in result.plan.market_plans),
                applied_action_count=len(result.applied_actions),
                failed_action_count=len(result.failed_actions),
                refresh_failures=self._last_refresh_summary.failures
                if self._last_refresh_summary is not None
                else (),
            )
            self._last_result = summary
            self._recent_results.append(summary)
            return result
        finally:
            self._running = False

    def status_snapshot(self) -> ReconcileWorkerStatus:
        return ReconcileWorkerStatus(
            running=self._running,
            last_trace_id=self._last_trace_id,
            last_trigger_event_type=self._last_trigger_event_type,
            last_started_at=self._last_started_at,
            last_completed_at=self._last_completed_at,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            last_refresh_summary=self._last_refresh_summary,
            last_result=self._last_result,
            recent_results=tuple(self._recent_results),
        )

    async def reconcile_once(
        self,
        *,
        trace_id: str | None = None,
        trigger_event: DomainEvent | None = None,
        condition_ids: tuple[str, ...] | None = None,
        refresh_market_authority: bool = True,
    ) -> ReconcileWorkerResult:
        trace_id = trace_id or (trigger_event.trace_id if trigger_event is not None else None) or uuid4().hex
        self._running = True
        self._last_started_at = _utc_now()
        self._last_trace_id = trace_id
        self._last_trigger_event_type = None if trigger_event is None else str(trigger_event.event_type)
        refresh_summary = await self._refresh_authoritative_state(
            trace_id=trace_id,
            condition_ids=condition_ids,
            refresh_market_authority=refresh_market_authority,
        )
        self._last_refresh_summary = refresh_summary
        if refresh_summary.failures:
            logger.warning(
                "reconcile authoritative refresh degraded",
                extra={
                    "trace_id": trace_id,
                    "failure_count": len(refresh_summary.failures),
                    "failures": refresh_summary.failures,
                },
            )
        registry_snapshot, account_snapshot = self._resolve_snapshots()
        plan = self._reconcile_service.build_reconcile_plan(
            registry_snapshot=registry_snapshot,
            account_snapshot=account_snapshot,
            trace_id=trace_id,
            condition_ids=condition_ids,
        )
        await self._publish(
            OutboxPriority.P3,
            DomainEvent(
                trace_id=trace_id,
                event_type=DomainEventType.RECONCILE_STARTED,
                event_id=uuid4().hex,
                reason="reconcile_started",
                created_at=_utc_now(),
                payload={
                    "market_count": len(plan.market_plans),
                    "paused_market_count": plan.paused_market_count,
                    "diff_count": plan.diff_count,
                    "trigger_event_type": None if trigger_event is None else str(trigger_event.event_type),
                    "refresh_summary": refresh_summary.as_payload(),
                },
            ),
        )

        applied_actions: list[ReconcileAction] = []
        failed_actions: list[tuple[ReconcileAction, str]] = []
        working_snapshot = account_snapshot

        # CLAUDE.md §7：reconciler 不长时间持有交易状态写锁；100+ market 时每
        # _RECONCILE_YIELD_EVERY 次让出 event loop，避免在批扫描里 starve P0。
        for market_index, market_plan in enumerate(plan.market_plans):
            if market_index > 0 and market_index % _RECONCILE_YIELD_EVERY == 0:
                await asyncio.sleep(0)
            if market_plan.pause_trading:
                # 状态去重:已 paused 的 market 不重复 publish.每 20s reconcile×
                # 200 market = 4000 重复事件/20s,实测占 audit 20%.
                # state 挂在 registry.companion(cid),prune 时自然消失,无内存泄漏.
                cid = market_plan.market.condition_id
                companion = self._registry.companion(cid) if self._registry else None
                if companion is not None and not companion.already_paused_audit:
                    companion.already_paused_audit = True
                    await self._publish(
                        OutboxPriority.P1,
                        DomainEvent(
                            trace_id=trace_id,
                            event_type=DomainEventType.TRADING_PAUSED_FOR_MARKET,
                            event_id=uuid4().hex,
                            market_slug=market_plan.market.market_slug,
                            condition_id=cid,
                            token_id=None,
                            reason=market_plan.pause_reason or "market_not_tradable",
                            created_at=_utc_now(),
                            payload={
                                "market_status": market_plan.market.trading_status.value,
                                "pause_reason": market_plan.pause_reason,
                            },
                        ),
                    )
                # reconcile pause 写 _market_pauses(RECONCILE source):
                # 同次扫描后 _prune_unsubscribable_markets 检测到 TERMINAL_LIVE_STATE_PAUSE_REASONS
                # → prune market.prune callback 同步 resume → 整轮事务结束后 cid 完全消失.
                # 前端短暂看到的"pause 状态"是合理瞬态语义,不是 bug.
                if self._account_state_store is not None:
                    self._account_state_store.pause_market(
                        market_plan.market.condition_id,
                        reason=market_plan.pause_reason or "market_not_tradable",
                        source=MarketPauseSource.RECONCILE,
                    )
                    working_snapshot = self._account_state_store.snapshot()

            for action in market_plan.actions:
                await self._publish_diff(trace_id, market_plan.market, action)
                try:
                    await self._action_applier.apply(action, market_plan.market, working_snapshot)
                    applied_actions.append(action)
                    if self._account_state_store is not None:
                        working_snapshot = self._account_state_store.snapshot()
                except Exception as exc:
                    failed_actions.append((action, str(exc)))

            if market_plan.has_changes:
                # 60s throttle:reconcile 每 20s 跑,大部分 cycle 都有微小 changes
                # (open_order TTL refresh / position fee accrual),逐条 publish 占
                # audit 18%.60s 颗粒度对复盘足够.关键 diff 由 RECONCILE_DIFF_DETECTED 单独 publish.
                import time as _time
                cid = market_plan.market.condition_id
                now_mono = _time.monotonic()
                companion = self._registry.companion(cid) if self._registry else None
                last_at = companion.last_reconcile_applied_at_mono if companion else 0.0
                if companion is not None and (now_mono - last_at) >= self._reconcile_applied_min_interval_s:
                    companion.last_reconcile_applied_at_mono = now_mono
                    await self._publish(
                        OutboxPriority.P3,
                        DomainEvent(
                            trace_id=trace_id,
                            event_type=DomainEventType.RECONCILE_APPLIED,
                            event_id=uuid4().hex,
                            market_slug=market_plan.market.market_slug,
                            condition_id=cid,
                            token_id=None,
                            reason="reconcile_applied",
                            created_at=_utc_now(),
                            payload={
                                "action_count": len(market_plan.actions),
                                "applied_count": sum(
                                    1 for item in applied_actions if item.condition_id == cid
                                ),
                                "failed_count": sum(
                                    1 for item, _ in failed_actions if item.condition_id == cid
                                ),
                            },
                        ),
                )

        if self._account_state_store is not None:
            self._account_state_store.mark_reconciled()
            await self._prune_unsubscribable_markets(self._account_state_store.snapshot())

        completed_at = _utc_now()
        self._last_completed_at = completed_at
        self._last_success_at = completed_at
        self._last_error = None

        if self._lifecycle_bus is not None:
            self._lifecycle_bus.publish(
                LifecycleEvent.RECONCILE_PASSED,
                trace_id=trace_id,
                payload={
                    "trace_id": trace_id,
                    "started_at": self._last_started_at.isoformat() if self._last_started_at else None,
                    "completed_at": completed_at.isoformat(),
                    "market_count": len(plan.market_plans),
                    "applied_action_count": len(applied_actions),
                    "failed_action_count": len(failed_actions),
                    "diff_count": plan.diff_count,
                },
            )

        return ReconcileWorkerResult(
            trace_id=trace_id,
            plan=plan,
            applied_actions=tuple(applied_actions),
            failed_actions=tuple(failed_actions),
        )

    async def _publish_diff(self, trace_id: str, market: Market, action: ReconcileAction) -> None:
        # 30s throttle 兜底:reconcile 每 20s 每 market 多个 action,1h 累 80k+ 条.
        # 关键 diff(订单 cancel/replace)走 ORDER 链路独立 audit,这里是状态级 diff
        # 描述,30s 颗粒度复盘足够.companion 挂 registry,prune 自动消失.
        import time as _time
        now_mono = _time.monotonic()
        companion = self._registry.companion(market.condition_id) if self._registry else None
        if companion is None:
            return  # cid 已 prune,跳过 audit
        if (now_mono - companion.last_reconcile_diff_at_mono) < self._reconcile_diff_min_interval_s:
            return
        companion.last_reconcile_diff_at_mono = now_mono
        await self._publish(
            OutboxPriority.P3,
            DomainEvent(
                trace_id=trace_id,
                event_type=DomainEventType.RECONCILE_DIFF_DETECTED,
                event_id=uuid4().hex,
                market_slug=market.market_slug,
                condition_id=market.condition_id,
                token_id=action.token_id,
                reason=action.reason,
                created_at=_utc_now(),
                payload={
                    "action_type": action.action_type.value,
                    "source_order_id": action.source_order_id,
                    "source_order_side": None if action.source_order_side is None else action.source_order_side.value,
                    "target_size_shares": None if action.target_size_shares is None else str(action.target_size_shares),
                    "target_notional_usdc": None if action.target_notional_usdc is None else str(action.target_notional_usdc),
                    "pause_reason": action.pause_reason,
                    "metadata": jsonable(action.metadata),
                },
            ),
        )

    async def _publish(self, priority: OutboxPriority, event: DomainEvent) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(priority, event)

    async def _prune_unsubscribable_markets(self, account_snapshot: AccountSnapshot) -> None:
        if self._registry is None:
            return
        now = _utc_now()
        markets = self._registry.snapshot().markets
        for market_index, market in enumerate(markets):
            # 每 _RECONCILE_YIELD_EVERY 个 market 让一次 loop——prune 调用 registry.remove_market
            # 会拿写锁；100+ market 串行循环会反向阻塞 P0。配合主循环的 yield 一并修复
            # CLAUDE.md §7「reconciler 不长时间持锁」。
            if market_index > 0 and market_index % _RECONCILE_YIELD_EVERY == 0:
                await asyncio.sleep(0)
            prune_reason = market_unsubscribe_prune_reason(
                account_snapshot,
                market,
                now=now,
            )
            if prune_reason is None:
                continue
            self._registry.remove_market(market.condition_id)
            if self._market_ws_worker is not None:
                self._market_ws_worker.untrack_market(market.token_ids)
            # 同步清 market_pauses[cid]:否则 market_pauses 内存泄漏(实测 90s +45),
            # registry 删了但 pause 还留着,长跑必爆.
            if self._account_state_store is not None:
                self._account_state_store.resume_market(market.condition_id)
            # audit dedupe state 自动消失:registry.remove_market(cid) 已联动 pop companion.
            logger.info(
                "pruned unsubscribable market from runtime tracking",
                extra={
                    "condition_id": market.condition_id,
                    "market_slug": market.market_slug,
                    "reason": prune_reason,
                },
            )

    def _resolve_snapshots(self) -> tuple[MarketRegistrySnapshot, AccountSnapshot]:
        if self._registry is not None:
            registry_snapshot = self._registry.snapshot()
        elif self._registry_snapshot_provider is not None:
            registry_snapshot = self._registry_snapshot_provider()
        else:
            registry_snapshot = MarketRegistrySnapshot(tuple())

        if self._account_state_store is not None:
            account_snapshot = self._account_state_store.snapshot()
        elif self._account_snapshot_provider is not None:
            account_snapshot = self._account_snapshot_provider()
        else:
            account_snapshot = AccountSnapshot()

        return registry_snapshot, account_snapshot

    async def _refresh_authoritative_state(
        self,
        *,
        trace_id: str,
        condition_ids: tuple[str, ...] | None = None,
        refresh_market_authority: bool = True,
    ) -> AuthoritativeRefreshSummary:
        return await self._authority_refresher.refresh(
            trace_id=trace_id,
            condition_ids=condition_ids,
            refresh_market_authority=refresh_market_authority,
        )


def _event_requests_market_authority_refresh(event: DomainEvent | None) -> bool:
    if event is None:
        return False
    return str(event.event_type).startswith("reconcile_")
