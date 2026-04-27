from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_trader.app.market_service import MarketService
from polymarket_trader.app.ports import bind_extension_orderbook_reader, build_extension_ports
from polymarket_trader.app.reconcile_service import ReconcileService
from polymarket_trader.app.extension_host import load_extension
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.config import ConfigIssue, ConfigLoadError, Settings, StartupReadiness, load_settings
from polymarket_trader.domain.events import DomainEvent, OutboxPriority
from polymarket_trader.infra.db import (
    AccountSnapshotRepository,
    DatabasePersistenceRepository,
    FillRepository,
    MarketRepository,
    OrderRepository,
    PositionRepository,
    build_session_factory,
)
from polymarket_trader.infra.outbox.local_queue import LocalOutbox
from polymarket_trader.infra.outbox import build_domain_event_outbox_sink
from polymarket_trader.infra.polymarket import (
    ClobClient,
    DataClient,
    GammaClient,
    PolymarketOrderExecutionClient,
    PolymarketTradingClient,
    PolymarketWebSocketClient,
    build_trading_client,
)
from polymarket_trader.infra.polymarket.order_executor import (
    InMemoryPolymarketOrderClient,
    PolymarketOrderExecutor,
)
from polymarket_trader.infra.sports import EspnScoreboardClient
from polymarket_trader.logging import LoggingRuntime, configure_logging
from polymarket_trader.observability.metrics import MetricsRegistry
from polymarket_trader.runtime import (
    RuntimePhase,
    Scheduler,
    Supervisor,
    WorkerLifecycleState,
    trading_gate_reason,
)
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.discovery_runner import (
    FullMarketDiscoveryState,
    MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS,
    MARKET_DISCOVERY_TICK_SECONDS,
    run_market_discovery_scan,
)
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.metrics_sync import sync_runtime_metrics as _sync_runtime_metrics
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.runtime.ws_loops import (
    handle_market_ws_message,
    run_market_ws as _run_market_ws,
    run_user_ws as _run_user_ws,
)
from polymarket_trader.extension_api import BusinessExtension
from polymarket_trader.workers.market_discovery_worker import MarketDiscoveryWorker
from polymarket_trader.workers.market_ws_worker import MarketWsWorker
from polymarket_trader.workers.persistence_worker import PersistenceWorker
from polymarket_trader.workers.reconcile_worker import ReconcileWorker
from polymarket_trader.workers.sports_live_state_worker import SportsLiveStateWorker
from polymarket_trader.workers.trading_decision_worker import TradingDecisionWorker
from polymarket_trader.workers.user_ws_worker import UserWsWorker

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeComponents:
    settings: Settings
    readiness: StartupReadiness
    extension: BusinessExtension
    logging_runtime: LoggingRuntime
    gamma_client: GammaClient
    clob_client: ClobClient
    data_client: DataClient
    trading_client: PolymarketTradingClient | None
    polymarket_ws_client: PolymarketWebSocketClient
    event_bus: EventBus
    registry: MarketRegistry
    outbox: LocalOutbox
    db_session_factory: async_sessionmaker[AsyncSession]
    persistence_repository: DatabasePersistenceRepository
    persistence_worker: PersistenceWorker
    account_state_store: AccountStateStore
    entry_metadata_store: EntryMetadataStore
    order_executor: PolymarketOrderExecutor
    market_ws_worker: MarketWsWorker
    user_ws_worker: UserWsWorker
    market_service: MarketService
    market_discovery_worker: MarketDiscoveryWorker
    market_discovery_scan: FullMarketDiscoveryState
    sports_live_state_client: EspnScoreboardClient | None
    sports_live_state_worker: SportsLiveStateWorker | None
    trading_decision_service: TradingDecisionService
    trading_service: TradingService
    trading_decision_worker: TradingDecisionWorker
    reconcile_service: ReconcileService
    reconcile_worker: ReconcileWorker
    scheduler: Scheduler
    supervisor: Supervisor
    metrics: MetricsRegistry
    trading_thread_pool: ThreadPoolExecutor
    maintenance_thread_pool: ThreadPoolExecutor
    maintenance_process_pool: ProcessPoolExecutor
    background_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    admin_service: object | None = None
    bootstrap_summary: dict[str, Any] = field(default_factory=dict)


def build_runtime(settings: Settings | None = None) -> RuntimeComponents:
    settings = settings or load_settings()
    readiness = settings.validate_startup_readiness()
    logging_runtime = configure_logging()
    metrics = MetricsRegistry()
    trading_thread_pool = ThreadPoolExecutor(
        max_workers=settings.trading_worker_threads,
        thread_name_prefix="trader-trading",
    )
    maintenance_thread_pool = ThreadPoolExecutor(
        max_workers=settings.maintenance_worker_threads,
        thread_name_prefix="trader-maintenance",
    )
    maintenance_process_pool = ProcessPoolExecutor(
        max_workers=settings.maintenance_process_workers,
    )
    trading_client = build_trading_client(settings)
    gamma_client = GammaClient(base_url=settings.polymarket_gamma_host)
    clob_client = ClobClient(
        base_url=settings.polymarket_clob_host,
        auth_client=trading_client,
    )
    data_client = DataClient(
        base_url=settings.polymarket_data_host,
        auth_client=trading_client,
    )
    polymarket_ws_client = PolymarketWebSocketClient(
        market_url=settings.polymarket_market_ws,
        user_url=settings.polymarket_user_ws,
    )
    event_bus = EventBus(
        trading_capacity=settings.trading_event_queue_max_size,
        maintenance_capacity=settings.maintenance_event_queue_max_size,
        persistence_capacity=settings.persistence_event_queue_max_size,
    )
    registry = MarketRegistry()
    entry_metadata_store = EntryMetadataStore()
    outbox = LocalOutbox(max_size=settings.persistence_event_queue_max_size)
    event_bus.bind_persistence_sink(build_domain_event_outbox_sink(outbox))
    db_session_factory = build_session_factory(settings.database_url)
    persistence_repository = DatabasePersistenceRepository(db_session_factory)
    persistence_worker = PersistenceWorker(
        outbox=outbox,
        repository=persistence_repository,
    )
    account_state_store = AccountStateStore()
    account_state_store.update_balances(
        balance_usdc=settings.portfolio_budget_usdc,
        allowance_usdc=settings.portfolio_budget_usdc,
    )
    extension_ports = build_extension_ports(
        registry=registry,
        snapshot_provider=account_state_store.snapshot,
    )
    if settings.extension_module is None:
        raise ConfigLoadError(
            [
                ConfigIssue(
                    field="extension_module",
                    code="missing_extension_module",
                    message="必须显式配置二次开发业务扩展模块。",
                )
            ]
        )
    extension = load_extension(
        module_path=settings.extension_module,
        ports=extension_ports,
        config_path=settings.extension_config_path,
    )
    execution_client = (
        PolymarketOrderExecutionClient(trading_client)
        if trading_client is not None
        else InMemoryPolymarketOrderClient()
    )
    order_executor = PolymarketOrderExecutor(
        client=execution_client,
        outbox=outbox,
        thread_pool=trading_thread_pool,
        sign_timeout_ms=settings.order_sign_timeout_ms,
        submit_timeout_ms=settings.order_submit_timeout_ms,
        critical_lock_timeout_ms=settings.critical_lock_timeout_ms,
    )

    async def load_market_rest_snapshot(token_id: str):
        orderbook = await clob_client.get_orderbook(token_id)
        return orderbook.to_snapshot()

    market_ws_worker = MarketWsWorker(
        event_bus=event_bus,
        registry=registry,
        rest_snapshot_loader=load_market_rest_snapshot,
    )
    bind_extension_orderbook_reader(extension_ports, market_ws_worker.snapshot)
    market_service = MarketService(
        extension_hooks=extension.hooks,
        registry=registry,
        market_tracker=market_ws_worker,
        account_snapshot_provider=account_state_store.snapshot,
    )
    trading_decision_service = TradingDecisionService(
        extension_hooks=extension.hooks,
        registry=registry,
        orderbook_reader=market_ws_worker.snapshot,
    )
    trading_service = TradingService(
        executor=order_executor,
    )
    user_ws_worker = UserWsWorker(
        event_bus=event_bus,
        account_state_store=account_state_store,
    )

    def entry_metadata_for_event(event, _snapshot):
        market = None
        if event.condition_id is not None:
            market = registry.get_by_condition_id(event.condition_id)
        if market is None and event.token_id is not None:
            market = registry.get_by_token_id(event.token_id)
        if market is None and event.market_slug is not None:
            market = registry.get_by_slug(event.market_slug)
        return entry_metadata_store.metadata_for_event(event, market=market)

    def entry_metadata_for_market(market):
        return entry_metadata_store.metadata_for(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
        )

    trading_decision_worker = TradingDecisionWorker(
        event_bus=event_bus,
        trading_decision_service=trading_decision_service,
        trading_service=trading_service,
        account_state_store=account_state_store,
        portfolio_budget_usdc=settings.portfolio_budget_usdc,
        max_order_usdc=settings.max_order_usdc,
        max_market_usdc=settings.max_market_usdc,
        max_total_usdc=settings.max_total_usdc,
        max_open_orders=settings.max_open_orders,
        order_retry_limit=settings.order_retry_limit,
        entry_metadata_provider=entry_metadata_for_event,
    )
    reconcile_service = ReconcileService(
        extension_hooks=extension.hooks,
        entry_metadata_provider=entry_metadata_for_market,
    )
    reconcile_worker = ReconcileWorker(
        event_bus=event_bus,
        reconcile_service=reconcile_service,
        registry_snapshot_provider=registry.snapshot,
        account_snapshot_provider=account_state_store.snapshot,
        account_state_store=account_state_store,
        trading_service=trading_service,
        registry=registry,
        market_ws_worker=market_ws_worker,
        gamma_client=gamma_client,
        clob_client=clob_client,
        data_client=data_client,
        trading_client=trading_client,
    )
    market_discovery_worker = MarketDiscoveryWorker(
        market_service=market_service,
        event_bus=event_bus,
        retry_delay_seconds=MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS,
    )
    market_discovery_scan = FullMarketDiscoveryState()
    sports_live_state_client: EspnScoreboardClient | None = None
    sports_live_state_worker: SportsLiveStateWorker | None = None
    sports_live_state_source = settings.sports_live_state_source.strip().lower()
    if settings.sports_live_state_enabled and sports_live_state_source == "espn":
        sports_live_state_client = EspnScoreboardClient(
            base_url=settings.sports_live_state_base_url,
            leagues=settings.sports_live_state_league_codes,
            timeout_s=settings.sports_live_state_timeout_s,
        )
        sports_live_state_matcher = getattr(extension.hooks, "match_sports_live_state", None)
        if callable(sports_live_state_matcher):
            sports_live_state_worker = SportsLiveStateWorker(
                snapshot_provider=sports_live_state_client.list_games,
                match_live_state=sports_live_state_matcher,
                registry=registry,
                entry_metadata_store=entry_metadata_store,
                event_bus=event_bus,
                enabled=True,
                source=sports_live_state_source,
                leagues=settings.sports_live_state_league_codes,
                publish_entry_signals=settings.sports_live_state_publish_entry_signals,
            )
        else:
            logger.warning(
                "sports live state sync disabled because extension lacks match_sports_live_state hook",
                extra={"extension": getattr(extension.spec, "name", "unknown")},
            )
    scheduler = Scheduler()
    supervisor = Supervisor(
        event_bus=event_bus,
        settings_readiness=readiness,
        scheduler_snapshot_provider=scheduler.snapshot,
        account_snapshot_provider=account_state_store.snapshot,
        market_ws_snapshot_provider=lambda: market_ws_worker.status_snapshot(include_subscriptions=False),
        user_ws_snapshot_provider=lambda: user_ws_worker.status_snapshot(include_subscriptions=False),
        reconcile_snapshot_provider=reconcile_worker.status_snapshot,
        persistence_snapshot_provider=persistence_worker.snapshot,
        metrics_snapshot_provider=metrics.snapshot,
        trading_queue_warn_depth=settings.trading_queue_warn_depth,
        entry_signal_to_submit_warn_ms=settings.entry_signal_to_submit_warn_ms,
        outbox_depth_warn=settings.persistence_event_queue_max_size,
        reconcile_stale_after_seconds=max(settings.market_sync_interval_seconds * 2, 60),
    )
    return RuntimeComponents(
        settings=settings,
        readiness=readiness,
        extension=extension,
        logging_runtime=logging_runtime,
        gamma_client=gamma_client,
        clob_client=clob_client,
        data_client=data_client,
        trading_client=trading_client,
        polymarket_ws_client=polymarket_ws_client,
        event_bus=event_bus,
        registry=registry,
        outbox=outbox,
        db_session_factory=db_session_factory,
        persistence_repository=persistence_repository,
        persistence_worker=persistence_worker,
        account_state_store=account_state_store,
        entry_metadata_store=entry_metadata_store,
        order_executor=order_executor,
        market_ws_worker=market_ws_worker,
        user_ws_worker=user_ws_worker,
        market_service=market_service,
        market_discovery_worker=market_discovery_worker,
        market_discovery_scan=market_discovery_scan,
        sports_live_state_client=sports_live_state_client,
        sports_live_state_worker=sports_live_state_worker,
        trading_decision_service=trading_decision_service,
        trading_service=trading_service,
        trading_decision_worker=trading_decision_worker,
        reconcile_service=reconcile_service,
        reconcile_worker=reconcile_worker,
        scheduler=scheduler,
        supervisor=supervisor,
        metrics=metrics,
        trading_thread_pool=trading_thread_pool,
        maintenance_thread_pool=maintenance_thread_pool,
        maintenance_process_pool=maintenance_process_pool,
    )


async def create_runtime(settings: Settings | None = None) -> RuntimeComponents:
    runtime = build_runtime(settings)
    try:
        await bootstrap_runtime(runtime)
    except Exception:
        with suppress(Exception):
            await shutdown_runtime(runtime)
        raise
    return runtime


async def bootstrap_runtime(runtime: RuntimeComponents) -> RuntimeComponents:
    runtime.supervisor.set_phase(RuntimePhase.CONFIG_LOADING)
    _register_runtime_workers(runtime)
    _seed_default_metrics(runtime)
    _sync_runtime_metrics(runtime)

    db_ready = await _check_database_connection(runtime.db_session_factory)
    runtime.supervisor.mark_db_ready(db_ready, reason="database_unavailable" if not db_ready else "")
    runtime.supervisor.mark_trading_client_ready(
        runtime.trading_client is not None and runtime.readiness.ready_to_trade,
        reason="trading_client_unavailable",
    )

    runtime.supervisor.set_phase(RuntimePhase.INFRA_READY)
    runtime.supervisor.set_phase(RuntimePhase.RECOVERING_SNAPSHOT)
    loaded_reference = await _load_reference_state(runtime)

    reconcile_summary: dict[str, Any]
    runtime.supervisor.set_phase(RuntimePhase.RECONCILING)
    try:
        result = await _run_reconcile_once(
            runtime,
            source="startup",
            refresh_market_authority=False,
        )
        reconcile_summary = {
            "trace_id": result.trace_id,
            "markets": len(result.plan.market_plans),
            "diff_count": result.plan.diff_count,
            "applied_actions": len(result.applied_actions),
            "failed_actions": len(result.failed_actions),
        }
    except Exception as exc:  # pragma: no cover - startup may fail on live dependencies
        reconcile_summary = {"error": str(exc)}
        runtime.supervisor.mark_degraded(f"startup_reconcile_failed:{exc}")
        logger.warning(
            "startup reconcile failed; runtime remains degraded",
            extra={"reason": str(exc)},
        )

    _start_background_tasks(runtime)
    _register_scheduler_jobs(runtime)
    runtime.scheduler.start_all()
    runtime.supervisor.set_phase(RuntimePhase.WORKERS_STARTED)
    _sync_runtime_metrics(runtime)
    snapshot = await runtime.supervisor.refresh()
    runtime.metrics.set_trading_gate(
        snapshot.automatic_trading_enabled,
        reason=trading_gate_reason(snapshot),
        source="supervisor",
    )
    runtime.bootstrap_summary.update(
        {
            "loaded_reference": loaded_reference,
            "startup_reconcile": reconcile_summary,
        }
    )
    if not snapshot.readiness or not snapshot.readiness.ready:
        logger.warning(
            "runtime bootstrapped in safe mode; automatic trading remains disabled",
            extra={"readiness": None if snapshot.readiness is None else snapshot.readiness.as_dict()},
        )
    return runtime


async def shutdown_runtime(runtime: RuntimeComponents) -> None:
    runtime.supervisor.set_phase(RuntimePhase.STOPPING)
    runtime.metrics.set_trading_gate(False, reason="shutdown", source="runtime")
    await runtime.scheduler.shutdown()

    runtime.persistence_worker.stop()
    tasks = list(runtime.background_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    runtime.background_tasks.clear()

    with suppress(Exception):
        await runtime.order_executor.aclose()
    for client in (runtime.gamma_client, runtime.clob_client, runtime.data_client):
        with suppress(Exception):
            await client.aclose()
    if runtime.sports_live_state_client is not None:
        with suppress(Exception):
            await runtime.sports_live_state_client.aclose()
    bind = getattr(runtime.db_session_factory, "kw", {}).get("bind")
    if bind is not None:
        with suppress(Exception):
            await bind.dispose()
    runtime.trading_thread_pool.shutdown(wait=False, cancel_futures=True)
    runtime.maintenance_thread_pool.shutdown(wait=False, cancel_futures=True)
    runtime.maintenance_process_pool.shutdown(wait=False, cancel_futures=True)
    runtime.logging_runtime.shutdown()
    runtime.supervisor.set_phase(RuntimePhase.STOPPED)


async def run() -> RuntimeComponents:
    return await create_runtime()


def main() -> None:
    asyncio.run(run())


def _register_runtime_workers(runtime: RuntimeComponents) -> None:
    runtime.supervisor.register_worker("admin_api", priority="P3", state=WorkerLifecycleState.RUNNING)
    runtime.supervisor.register_worker("market_discovery", priority="P2")
    runtime.supervisor.register_worker("market_ws", priority="P0", state=WorkerLifecycleState.PAUSED)
    runtime.supervisor.register_worker("user_ws", priority="P0", state=WorkerLifecycleState.PAUSED)
    runtime.supervisor.register_worker("trading_decision", priority="P0")
    runtime.supervisor.register_worker("reconcile", priority="P2")
    if runtime.sports_live_state_worker is not None:
        runtime.supervisor.register_worker("sports_live_state_sync", priority="P2")
    runtime.supervisor.register_worker("persistence", priority="P3")


def _seed_default_metrics(runtime: RuntimeComponents) -> None:
    for gauge_name in (
        "entry_signal_to_submit_ms",
        "trading_lock_wait_ms",
        "executor_queue_wait_ms",
        "ws_event_lag_ms",
    ):
        runtime.metrics.set_gauge(gauge_name, 0.0)
    runtime.metrics.set_trading_gate(False, reason="bootstrap", source="runtime")


async def _check_database_connection(
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - depends on external db
        logger.warning("database readiness check failed", extra={"reason": str(exc)})
        return False


def _restore_account_reference_state(runtime: RuntimeComponents, *, balance_usdc, allowance_usdc) -> None:
    runtime.account_state_store.update_balances(
        balance_usdc=balance_usdc,
        allowance_usdc=allowance_usdc,
    )


async def _handle_market_ws_message(runtime, message) -> None:
    await handle_market_ws_message(runtime, message)


async def _load_reference_state(runtime: RuntimeComponents) -> dict[str, int]:
    loaded = {"markets": 0, "positions": 0, "open_orders": 0, "fills": 0, "account_snapshots": 0}
    try:
        async with runtime.db_session_factory() as session:
            account_snapshot = await AccountSnapshotRepository(session).get_current_snapshot()
            markets = await MarketRepository(session).list_markets_snapshot(limit=500, offset=0)
            positions = await PositionRepository(session).list_positions_snapshot(limit=500, offset=0)
            open_orders = await OrderRepository(session).list_open_orders_snapshot(limit=500, offset=0)
            fills = await FillRepository(session).list_fills_snapshot(limit=500, offset=0)
        if account_snapshot is not None:
            _restore_account_reference_state(
                runtime,
                balance_usdc=account_snapshot.balance_usdc,
                allowance_usdc=account_snapshot.allowance_usdc,
            )
        for market in markets.items:
            runtime.market_ws_worker.track_market(market)
        runtime.account_state_store.replace_positions(positions.items)
        runtime.account_state_store.replace_open_orders(open_orders.items)
        runtime.account_state_store.replace_fills(fills.items)
        loaded = {
            "account_snapshots": 0 if account_snapshot is None else 1,
            "markets": len(markets.items),
            "positions": len(positions.items),
            "open_orders": len(open_orders.items),
            "fills": len(fills.items),
        }
    except Exception as exc:  # pragma: no cover - depends on external db
        logger.warning("failed to load reference state from database", extra={"reason": str(exc)})
    return loaded


def _start_background_tasks(runtime: RuntimeComponents) -> None:
    runtime.background_tasks["market_ws"] = asyncio.create_task(
        _run_supervised_loop(
            runtime,
            name="market_ws",
            runner=lambda: _run_market_ws(runtime),
        ),
        name="trader:market-ws",
    )
    runtime.background_tasks["user_ws"] = asyncio.create_task(
        _run_supervised_loop(
            runtime,
            name="user_ws",
            runner=lambda: _run_user_ws(runtime),
        ),
        name="trader:user-ws",
    )
    runtime.background_tasks["trading_decision"] = asyncio.create_task(
        _run_supervised_loop(
            runtime,
            name="trading_decision",
            runner=runtime.trading_decision_worker.run,
        ),
        name="trader:trading-decision",
    )
    runtime.background_tasks["reconcile"] = asyncio.create_task(
        _run_supervised_loop(
            runtime,
            name="reconcile",
            runner=lambda: _run_reconcile(runtime),
        ),
        name="trader:reconcile",
    )
    runtime.background_tasks["persistence"] = asyncio.create_task(
        _run_supervised_loop(
            runtime,
            name="persistence",
            runner=runtime.persistence_worker.run,
        ),
        name="trader:persistence",
    )


def _register_scheduler_jobs(runtime: RuntimeComponents) -> None:
    runtime.scheduler.register_job(
        "market_discovery_scan",
        lambda: _run_market_discovery_scan(runtime),
        priority="P2",
        interval_seconds=MARKET_DISCOVERY_TICK_SECONDS,
        tags=("market_discovery",),
        start=True,
        run_immediately=True,
    )
    runtime.scheduler.register_job(
        "periodic_reconcile",
        lambda: _publish_reconcile_trigger(runtime, source="scheduled"),
        priority="P2",
        interval_seconds=float(runtime.settings.market_sync_interval_seconds),
        tags=("reconcile",),
        start=True,
        run_immediately=False,
    )
    if runtime.sports_live_state_worker is not None:
        runtime.scheduler.register_job(
            "sports_live_state_sync",
            lambda: _run_sports_live_state_sync(runtime),
            priority="P2",
            interval_seconds=float(runtime.settings.sports_live_state_interval_seconds),
            tags=("sports_live_state",),
            start=True,
            run_immediately=True,
        )
    runtime.scheduler.register_job(
        "supervisor_refresh",
        lambda: _run_supervisor_refresh(runtime),
        priority="P2",
        interval_seconds=5.0,
        tags=("supervisor", "metrics"),
        start=True,
        run_immediately=True,
    )


async def _run_supervised_loop(
    runtime: RuntimeComponents,
    *,
    name: str,
    runner: Any,
) -> None:
    runtime.supervisor.heartbeat_worker(name, detail="starting")
    try:
        await runner()
    except asyncio.CancelledError:
        runtime.supervisor.heartbeat_worker(
            name,
            state=WorkerLifecycleState.STOPPED,
            healthy=True,
            detail="cancelled",
        )
        raise
    except Exception as exc:
        runtime.supervisor.mark_worker_error(name, detail="loop_failed", last_error=str(exc))
        raise


async def _run_market_discovery_scan(runtime: RuntimeComponents) -> None:
    await run_market_discovery_scan(runtime, sync_runtime_metrics=_sync_runtime_metrics)


async def _publish_reconcile_trigger(
    runtime: RuntimeComponents,
    *,
    source: str,
) -> None:
    await runtime.event_bus.publish(
        OutboxPriority.P2,
        DomainEvent(
            trace_id=f"reconcile-trigger-{source}-{uuid4().hex}",
            event_type=f"reconcile_{source}",
            event_id=uuid4().hex,
            reason=source,
            payload={"source": source},
        ),
    )
    _sync_runtime_metrics(runtime)


def _is_reconcile_trigger(event: DomainEvent) -> bool:
    event_type = str(event.event_type)
    if event_type.startswith("reconcile_"):
        return True
    return event_type == "market_resolved_or_disabled"


def _coalesce_reconcile_scope(
    events: tuple[DomainEvent, ...],
) -> tuple[str, DomainEvent, tuple[str, ...] | None]:
    trigger = events[-1]
    source_event_types = {str(event.event_type) for event in events}
    if len(source_event_types) == 1:
        source = f"event:{next(iter(source_event_types))}"
    else:
        source = f"event-batch:{len(events)}"

    condition_ids: list[str] = []
    seen_condition_ids: set[str] = set()
    for event in events:
        condition_id = getattr(event, "condition_id", None)
        if not condition_id:
            return source, trigger, None
        if condition_id in seen_condition_ids:
            continue
        seen_condition_ids.add(condition_id)
        condition_ids.append(condition_id)
    return source, trigger, tuple(condition_ids)


def _events_request_market_authority_refresh(events: tuple[DomainEvent, ...]) -> bool:
    return any(str(event.event_type).startswith("reconcile_") for event in events)


async def _run_reconcile(runtime: RuntimeComponents) -> None:
    max_batch_size = max(1, min(runtime.settings.maintenance_event_queue_max_size, 256))
    while True:
        first_trigger = await runtime.event_bus.next_maintenance_event()
        pending_events: list[DomainEvent] = [first_trigger]
        while (
            runtime.event_bus.maintenance_queue_depth() > 0
            and len(pending_events) < max_batch_size
        ):
            pending_events.append(await runtime.event_bus.next_maintenance_event())

        actionable_events = tuple(event for event in pending_events if _is_reconcile_trigger(event))
        if not actionable_events:
            _sync_runtime_metrics(runtime)
            await asyncio.sleep(0)
            continue

        source, trigger, condition_ids = _coalesce_reconcile_scope(actionable_events)
        await _run_reconcile_once(
            runtime,
            source=source,
            trigger_event=trigger,
            condition_ids=condition_ids,
            refresh_market_authority=_events_request_market_authority_refresh(actionable_events),
        )
        await asyncio.sleep(0)


async def _run_reconcile_once(
    runtime: RuntimeComponents,
    *,
    source: str,
    trigger_event: DomainEvent | None = None,
    condition_ids: tuple[str, ...] | None = None,
    refresh_market_authority: bool = True,
) -> Any:
    runtime.supervisor.heartbeat_worker("reconcile", detail=source)
    started_at = asyncio.get_running_loop().time()
    try:
        result = await runtime.reconcile_worker.reconcile_once(
            trace_id=f"reconcile-{source}-{uuid4().hex}",
            trigger_event=trigger_event,
            condition_ids=condition_ids,
            refresh_market_authority=refresh_market_authority,
        )
    except Exception as exc:
        duration_ms = (asyncio.get_running_loop().time() - started_at) * 1000.0
        runtime.metrics.record_reconcile(
            duration_ms,
            started_at=runtime.reconcile_worker.status_snapshot().last_started_at,
            completed_at=None,
            status="failed",
            error=str(exc),
            trace_id=runtime.reconcile_worker.status_snapshot().last_trace_id,
            actions=0,
        )
        runtime.supervisor.mark_worker_error("reconcile", detail="reconcile_failed", last_error=str(exc))
        _sync_runtime_metrics(runtime)
        raise
    duration_ms = (asyncio.get_running_loop().time() - started_at) * 1000.0
    status = runtime.reconcile_worker.status_snapshot()
    runtime.metrics.record_reconcile(
        duration_ms,
        started_at=status.last_started_at,
        completed_at=status.last_completed_at,
        status="ok",
        error=None,
        trace_id=status.last_trace_id,
        actions=len(result.applied_actions),
    )
    runtime.supervisor.heartbeat_worker(
        "reconcile",
        detail=f"applied={len(result.applied_actions)} failed={len(result.failed_actions)}",
    )
    _sync_runtime_metrics(runtime)
    return result


async def _run_sports_live_state_sync(runtime: RuntimeComponents) -> None:
    worker = runtime.sports_live_state_worker
    if worker is None:
        return
    runtime.supervisor.heartbeat_worker("sports_live_state_sync", detail="syncing")
    try:
        result = await worker.sync_once()
    except Exception as exc:
        runtime.supervisor.mark_worker_error(
            "sports_live_state_sync",
            detail="sync_failed",
            last_error=str(exc),
        )
        _sync_runtime_metrics(runtime)
        raise
    if result is None:
        runtime.supervisor.heartbeat_worker(
            "sports_live_state_sync",
            state=WorkerLifecycleState.PAUSED,
            detail="disabled",
        )
    else:
        runtime.supervisor.heartbeat_worker(
            "sports_live_state_sync",
            detail=(
                f"games={result.games_seen} matches={result.matches} "
                f"signals={result.entry_signals_published}"
            ),
        )
    _sync_runtime_metrics(runtime)


async def _run_supervisor_refresh(runtime: RuntimeComponents) -> None:
    _sync_runtime_metrics(runtime)
    snapshot = await runtime.supervisor.refresh()
    runtime.metrics.set_trading_gate(
        snapshot.automatic_trading_enabled,
        reason=trading_gate_reason(snapshot),
        source="supervisor",
    )


if __name__ == "__main__":
    main()
