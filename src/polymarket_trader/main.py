from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.app.operator_service import OperatorService

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from polymarket_trader.api.routes.stream import SseSubscriptionRegistry
from polymarket_trader.app.audit_retention import purge_audit_events_once
from polymarket_trader.app.dead_records_retention import purge_dead_records_once
from polymarket_trader.app.entry_metadata_providers import (
    build_entry_metadata_for_event_provider,
    build_entry_metadata_for_market_provider,
)
from polymarket_trader.pipeline.ingest.market_discovery.ingest_service import MarketIngestService
from polymarket_trader.app.ports import bind_runtime_season_state, build_runtime_ports
from polymarket_trader.recovery.reconcile_service import ReconcileService
from polymarket_trader.recovery.settlement_scanner import SettlementScannerService
from polymarket_trader.pipeline.decision.decision_context_builder import DecisionContextBuilder
from polymarket_trader.pipeline.execution.order_gateway import OrderGateway
from polymarket_trader.config import ConfigLoadError, Settings, StartupReadiness, load_settings
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.infra.db import (
    AccountSnapshotRepository,
    DatabasePersistenceRepository,
    build_engine,
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
from polymarket_trader.app.paper import PaperVirtualLedger
from polymarket_trader.infra.sports.goalserve_lazy_client import GoalserveLazyClient
from polymarket_trader.infra.sports import (
    GoalserveInplayClient,
    GoalserveLivescoreClient,
    GoalservePregameOddsClient,
    SeasonOddsClient,
    TheOddsApiClient,
)
from polymarket_trader.pipeline.ingest.live_source import (
    LiveSourceCalibrator,
    LiveSourceFeeder,
    LiveSourceLifecycleBinder,
    LiveSourceMatcher,
    LiveSourceProvider,
    LiveSourceRegistry,
    LiveStateMatchService,
    LiveStateStore,
    SportsSubscriptionPolicy,
    is_market_live_active,
)
from polymarket_trader.workflow.live_state import _market_sport_codes
from polymarket_trader.storage.season_state_store import SeasonStateStore
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
from polymarket_trader.runtime.data_graph import DataGraph
from polymarket_trader.runtime.market_metadata import MarketMetadataStore
from polymarket_trader.pipeline.ingest.market_discovery.discovery_runner import (
    FullMarketDiscoveryState,
    MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS,
    MARKET_DISCOVERY_TICK_SECONDS,
    run_market_discovery_scan,
)
from polymarket_trader.app.decision_recorder import DecisionEventRecorder
from polymarket_trader.app.parameter_store import ParameterStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.lifecycle_bus import InProcessLifecycleBus
from polymarket_trader.runtime.lifecycle_registry import LifecycleRegistry
from polymarket_trader.runtime.metrics_sync import sync_runtime_metrics as _sync_runtime_metrics
from polymarket_trader.runtime.orderbook_delta import OrderbookDeltaStore
from polymarket_trader.runtime.orderbook_derived_publisher import OrderbookDerivedPublisher
from polymarket_trader.runtime.orderbook_derived_store import OrderbookDerivedStore
from polymarket_trader.runtime.orderbook_history_buffer import OrderbookHistoryBuffer
from polymarket_trader.runtime.gamma_snapshot_store import GammaMarketSnapshotStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.runtime.ws_loops import (
    handle_market_ws_message,
    run_market_ws as _run_market_ws,
    run_user_ws as _run_user_ws,
)
from polymarket_trader.pipeline.ingest.market_discovery.discovery_worker import MarketDiscoveryWorker
from polymarket_trader.pipeline.ingest.orderbook_ws import MarketWsWorker
from polymarket_trader.app.audit import AuditDeduper, GarbageFilter, PersistenceWorker
from polymarket_trader.api.ws_admin import AdminWsPublisher
from polymarket_trader.runtime.observability_bridge import ObservabilityBridge
from polymarket_trader.recovery import ReconcileWorker, ReconcileWorkerResult
from polymarket_trader.pipeline.ingest.odds.sports_season_odds_worker import SportsSeasonOddsWorker
from polymarket_trader.pipeline.ingest.odds.game_odds_worker import GameOddsWorker
from polymarket_trader.pipeline.ingest.odds.goalserve_pregame_worker import GoalservePregameWorker
from polymarket_trader.pipeline.decision import MarketTickWorker
from polymarket_trader.workflow.config import load_workflow_config
from polymarket_trader.workflow.workflow import TradingWorkflow
from polymarket_trader.pipeline.feedback.user_ws import UserWsWorker
from polymarket_trader.infra.sports.game_odds_client import (
    GameOddsClient,
    TheOddsApiGameOddsClient,
)
from polymarket_trader.runtime.paper_runtime import build_paper_runtime
from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor

logger = logging.getLogger(__name__)

# 聚合器超时 = 单次 provider 超时 × 倍率，floor 防止 timeout_s 配置极小时完全没预算。
_PROVIDER_TIMEOUT_MULTIPLIER = 3
_PROVIDER_TIMEOUT_FLOOR_S = 12.0
# 启动快照加载上限（positions/orders/fills），限制重启时的内存占用。
# 启动快照加载上限（positions/orders/fills）,限制重启时的内存占用。
# market 不再从 DB 恢复——见 _load_reference_state 注释。
_STARTUP_SNAPSHOT_ITEM_LIMIT = 500
# reconcile 批次上限，防止 maintenance 队列积压时单批过大阻塞主循环。
_RECONCILE_BATCH_SIZE_LIMIT = 256


@dataclass(slots=True)
class RuntimeComponents:
    settings: Settings
    readiness: StartupReadiness
    workflow: TradingWorkflow
    logging_runtime: LoggingRuntime
    gamma_client: GammaClient
    clob_client: ClobClient
    data_client: DataClient
    trading_client: PolymarketTradingClient | None
    polymarket_ws_client: PolymarketWebSocketClient
    event_bus: EventBus
    registry: MarketRegistry
    lifecycle_registry: LifecycleRegistry
    gamma_snapshot_store: GammaMarketSnapshotStore
    outbox: LocalOutbox
    db_engine: AsyncEngine
    db_session_factory: async_sessionmaker[AsyncSession]
    persistence_repository: DatabasePersistenceRepository
    persistence_worker: PersistenceWorker
    audit_garbage_filter: GarbageFilter
    audit_deduper: AuditDeduper
    observability_bridge: ObservabilityBridge
    admin_ws_publisher: AdminWsPublisher
    account_state_store: AccountStateStore
    market_metadata_store: MarketMetadataStore
    order_executor: PolymarketOrderExecutor
    market_ws_worker: MarketWsWorker
    orderbook_delta_store: OrderbookDeltaStore
    orderbook_history_buffer: OrderbookHistoryBuffer
    data_graph: DataGraph
    orderbook_derived_store: OrderbookDerivedStore
    orderbook_derived_publisher: OrderbookDerivedPublisher
    user_ws_worker: UserWsWorker
    market_ingest_service: MarketIngestService
    market_discovery_worker: MarketDiscoveryWorker
    market_discovery_scan: FullMarketDiscoveryState
    live_state_store: LiveStateStore
    live_source_registry: LiveSourceRegistry
    live_source_match_service: LiveStateMatchService
    live_source_lifecycle_binder: LiveSourceLifecycleBinder
    live_source_feeders: tuple[LiveSourceFeeder, ...]
    # goalserve client aclose 列表（shutdown 时 await 调用，feeder 自身用 stop()）
    live_source_closers: tuple[Any, ...]
    decision_context_builder: DecisionContextBuilder
    order_gateway: OrderGateway
    market_tick_worker: MarketTickWorker
    reconcile_service: ReconcileService
    reconcile_worker: ReconcileWorker
    scheduler: Scheduler
    supervisor: Supervisor
    metrics: MetricsRegistry
    trading_thread_pool: ThreadPoolExecutor
    maintenance_thread_pool: ThreadPoolExecutor
    maintenance_process_pool: ProcessPoolExecutor
    background_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    operator_service: OperatorService | None = None
    sse_subscription_registry: SseSubscriptionRegistry | None = None
    bootstrap_summary: dict[str, Any] = field(default_factory=dict)
    season_state_store: SeasonStateStore | None = None
    season_odds_worker: SportsSeasonOddsWorker | None = None
    season_odds_client: SeasonOddsClient | None = None
    game_odds_worker: GameOddsWorker | None = None
    game_odds_client: GameOddsClient | None = None
    pregame_worker: GoalservePregameWorker | None = None
    pregame_client: GoalservePregameOddsClient | None = None
    parameter_store: ParameterStore | None = None
    # paper 模式虚拟账本（paper_trading_mode=true 时注入）。
    # admin API /runtime/paper-ledger 暴露 ledger.available_usdc / positions /
    # 累计 fee / 已实现 + 浮动 PnL，复盘必查。
    paper_ledger: "PaperVirtualLedger | None" = None
    goalserve_lazy_client: "GoalserveLazyClient | None" = None


def _build_live_source_components(
    settings: Settings,
    *,
    registry: MarketRegistry,
    market_metadata_store: MarketMetadataStore,
    event_bus: EventBus,
    workflow: TradingWorkflow,
) -> tuple[
    LiveStateStore,
    LiveSourceRegistry,
    LiveStateMatchService,
    LiveSourceLifecycleBinder,
    tuple[LiveSourceFeeder, ...],
    tuple[Any, ...],
]:
    """构造直播源 7 件套 + feeder × N + goalserve client × N。

    返回 (store, registry, service, binder, feeders, closers)。closers 用于
    shutdown 时调 goalserve client.aclose（feeder 自身用 stop()）。

    `sports_live_state_enabled=False` 时返回空 feeders/closers，但仍构造 store /
    registry / service / binder——它们是无状态依赖，admin/discovery 读取也安全。
    """

    live_state_store = LiveStateStore()
    live_source_registry = LiveSourceRegistry()

    def _primary_sport(market: Market) -> str | None:
        codes = _market_sport_codes(market)
        return next(iter(sorted(codes)), None)

    subscription_policy = SportsSubscriptionPolicy(
        sport_resolver=_primary_sport,
        active_predicate=is_market_live_active,
    )
    lifecycle_binder = LiveSourceLifecycleBinder(
        market_registry=registry,
        live_source_registry=live_source_registry,
        subscription_policy=subscription_policy,
    )
    matcher = LiveSourceMatcher(match_hook=workflow.match_live_state)
    calibrator = LiveSourceCalibrator()
    match_service = LiveStateMatchService(
        store=live_state_store,
        registry=live_source_registry,
        market_registry=registry,
        market_metadata_store=market_metadata_store,
        matcher=matcher,
        calibrator=calibrator,
        event_bus=event_bus,
    )

    feeders: list[LiveSourceFeeder] = []
    closers: list[Any] = []

    if not settings.sports_live_state_enabled:
        return (
            live_state_store,
            live_source_registry,
            match_service,
            lifecycle_binder,
            (),
            (),
        )

    # inplay (keyless, 8 sports，覆盖足球/篮球/网球/排球/美式足球/电竞/冰球/棒球)
    inplay_client = GoalserveInplayClient(
        proxy=settings.goalserve_proxy,
        active_sports_provider=lifecycle_binder.active_sports_provider(
            LiveSourceProvider.GOALSERVE_INPLAY
        ),
    )
    closers.append(inplay_client.aclose)
    feeders.append(
        LiveSourceFeeder(
            provider=LiveSourceProvider.GOALSERVE_INPLAY,
            snapshot_fetcher=inplay_client.list_events,
            expected_sports_provider=lambda: live_source_registry.active_sports_for(
                LiveSourceProvider.GOALSERVE_INPLAY
            ),
            store=live_state_store,
        )
    )

    # livescore (API key, 覆盖 inplay 不支持的运动: cricket/golf/horse_racing/f1/motogp/...)
    api_key_secret = settings.goalserve_api_key
    if settings.goalserve_livescore_enabled and api_key_secret is not None:
        livescore_client = GoalserveLivescoreClient(
            api_key=api_key_secret.get_secret_value(),
            base_url=settings.goalserve_livescore_base_url,
            timeout_s=settings.goalserve_livescore_timeout_s,
            poll_interval_s=float(settings.sports_live_state_interval_seconds),
            proxy=settings.goalserve_proxy,
            active_sports_provider=lifecycle_binder.active_sports_provider(
                LiveSourceProvider.GOALSERVE_LIVESCORE
            ),
        )
        closers.append(livescore_client.aclose)
        feeders.append(
            LiveSourceFeeder(
                provider=LiveSourceProvider.GOALSERVE_LIVESCORE,
                snapshot_fetcher=livescore_client.list_events,
                expected_sports_provider=lambda: live_source_registry.active_sports_for(
                    LiveSourceProvider.GOALSERVE_LIVESCORE
                ),
                store=live_state_store,
            )
        )

    return (
        live_state_store,
        live_source_registry,
        match_service,
        lifecycle_binder,
        tuple(feeders),
        tuple(closers),
    )


def _build_season_odds_worker(
    settings: Settings,
    *,
    registry: MarketRegistry,
    market_metadata_store: MarketMetadataStore,
    workflow: TradingWorkflow,
    event_bus: EventBus | None = None,
) -> tuple[SportsSeasonOddsWorker, SeasonOddsClient] | tuple[None, None]:
    """按 settings 装配 sports_season_odds_worker；缺 api_key 或未启用 outright 时返回 (None, None)。

    第二项是底层 httpx-backed odds client，用于运行时 shutdown 时关闭。
    """

    token_secret = settings.sports_season_odds_api_key
    api_key = token_secret.get_secret_value() if token_secret is not None else None
    if not api_key or settings.sports_season_odds_provider != "theoddsapi":
        return None, None
    client = TheOddsApiClient(
        api_key=api_key,
        base_url=settings.sports_season_odds_base_url,
        regions=tuple(
            r.strip().lower()
            for r in settings.sports_season_odds_regions.split(",")
            if r.strip()
        ),
    )
    _classifier = workflow

    def _is_outright(market: Market) -> bool:
        return _classifier.is_outright_market(market) if _classifier is not None else False

    def _sport_key(market: Market) -> str | None:
        return _classifier.sport_key_for_season_odds(market) if _classifier is not None else None

    def _market_key(market: Market) -> str:
        return market.event_slug or market.market_slug or ""

    worker = SportsSeasonOddsWorker(
        odds_client=client,
        registry=registry,
        market_metadata_store=market_metadata_store,
        sport_key_for=_sport_key,
        is_outright_market=_is_outright,
        market_key_for=_market_key,
        ttl_seconds=settings.sports_season_odds_ttl_seconds,
        enabled=True,
        event_bus=event_bus,
    )
    return worker, client


def _build_game_odds_worker(
    settings: Settings,
    *,
    registry: MarketRegistry,
    market_metadata_store: MarketMetadataStore,
    workflow: TradingWorkflow,
    event_bus: EventBus | None = None,
) -> tuple[GameOddsWorker, GameOddsClient] | tuple[None, None]:
    """按 settings 装配 game_odds_worker；缺 api_key 时返回 (None, None)。"""

    token_secret = settings.sports_game_odds_api_key
    api_key = token_secret.get_secret_value() if token_secret is not None else None
    if not api_key or settings.sports_game_odds_provider != "theoddsapi":
        return None, None
    client = TheOddsApiGameOddsClient(
        api_key=api_key,
        base_url=settings.sports_game_odds_base_url,
        regions=tuple(
            r.strip().lower()
            for r in settings.sports_game_odds_regions.split(",")
            if r.strip()
        ),
    )

    _classifier = workflow

    def _sport_key(market: Market) -> str | None:
        return _classifier.sport_key_for_game_odds(market) if _classifier is not None else None

    def _is_series_winner(market: Market) -> bool:
        return _classifier.is_series_winner_market(market) if _classifier is not None else False

    def _game_key(market: Market) -> str | None:
        # 与 series_state 同源 key：让两个 worker 用同一标识，便于审计串联。
        return market.event_slug or market.market_slug or None

    worker = GameOddsWorker(
        client=client,
        registry=registry,
        market_metadata_store=market_metadata_store,
        sport_key_for=_sport_key,
        is_series_winner_market=_is_series_winner,
        game_key_for=_game_key,
        ttl_seconds=settings.sports_game_odds_ttl_seconds,
        enabled=True,
        event_bus=event_bus,
    )
    return worker, client


def _build_pregame_worker(
    settings: Settings,
    *,
    registry: MarketRegistry,
    market_metadata_store: MarketMetadataStore,
) -> tuple[GoalservePregameWorker, GoalservePregameOddsClient] | tuple[None, None]:
    """按 settings 装配 goalserve_pregame_worker；未启用或无 API key 时返回 (None, None)。"""

    if not settings.goalserve_pregame_enabled:
        return None, None
    api_key_secret = settings.goalserve_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret is not None else None
    if not api_key:
        logger.warning("goalserve_pregame_worker: skipped — GOALSERVE_API_KEY not set")
        return None, None
    client = GoalservePregameOddsClient(
        api_key=api_key,
        sports=settings.goalserve_pregame_sport_codes,
        base_url=settings.goalserve_pregame_base_url,
        timeout_s=settings.goalserve_pregame_timeout_s,
        proxy=settings.goalserve_proxy,
    )
    worker = GoalservePregameWorker(
        client=client,
        enabled=True,
        registry=registry,
        market_metadata_store=market_metadata_store,
    )
    return worker, client


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
        user_address=settings.polymarket_funder_address,
    )
    data_client = DataClient(
        base_url=settings.polymarket_data_host,
        auth_client=trading_client,
        default_user_address=settings.polymarket_funder_address,
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
    # LifecycleRegistry 把 9+ 处散落的 market prune/added 回调收敛到统一 fan-out
    # 中枢。registry 只把 emit_pruned/added 注册一次到 prune/added callback，所有
    # 业务 listener 通过 lifecycle_registry.register_*_listener("name", cb) 注册。
    lifecycle_registry = LifecycleRegistry()
    registry.register_prune_callback(lifecycle_registry.emit_pruned)
    registry.register_added_callback(lifecycle_registry.emit_added)
    gamma_snapshot_store = GammaMarketSnapshotStore()
    market_metadata_store = MarketMetadataStore()
    outbox = LocalOutbox(max_size=settings.persistence_event_queue_max_size)
    event_bus.bind_persistence_sink(build_domain_event_outbox_sink(outbox))
    sse_subscription_registry = SseSubscriptionRegistry(soft_cap=settings.sse_subscriber_cap)
    event_bus.add_broadcast_listener(sse_subscription_registry.broadcast)
    db_engine = build_engine(settings.database_url)
    db_session_factory = build_session_factory(settings.database_url)
    persistence_repository = DatabasePersistenceRepository(db_session_factory)
    account_state_store = AccountStateStore()
    # 不预填假 balance：reconcile worker 会在启动后立刻调
    # ``clob_client.get_balance_allowance()`` 写入真实链上 USDC。在 reconcile
    # 拿到第一个权威值前，bankroll=0 → Kelly 全拒，正是安全态。
    lifecycle_bus = InProcessLifecycleBus()
    parameter_store = ParameterStore(event_bus=event_bus)
    runtime_ports = build_runtime_ports(
        lifecycle_bus=lifecycle_bus,
        parameter_store=parameter_store,
        metrics_registry=metrics,
    )
    # 直接装配 quant 策略——量化决策器就是这个交易系统本身。
    workflow_config = load_workflow_config(settings.workflow_config_path)
    workflow = TradingWorkflow(config=workflow_config, ports=runtime_ports)
    workflow_issues = workflow.validate_config(settings)
    if workflow_issues:
        raise ConfigLoadError(list(workflow_issues))
    parameter_store.bind_settings(settings)
    # §13.3 写入侧 dedupe + garbage filter——PersistenceWorker 入口同步过滤后
    # 才走 _persist_batch，避免高频重复 / synthetic heartbeat / debug 事件污染
    # audit_events 表（CLAUDE.md §0 + §13.7 payload <2KB 限制）
    audit_garbage_filter = GarbageFilter()
    audit_deduper = AuditDeduper()
    persistence_worker = PersistenceWorker(
        outbox=outbox,
        repository=persistence_repository,
        garbage_filter=audit_garbage_filter,
        deduper=audit_deduper,
    )
    async def load_market_rest_snapshot(token_id: str):
        orderbook = await clob_client.get_orderbook(token_id)
        return orderbook.to_snapshot()

    orderbook_delta_store = OrderbookDeltaStore()
    # 纯时间窗 15s(覆盖 2/3/5/10s + 余量),无 maxlen 兜底.
    # max_tokens=2000 LRU evict 防极端 token 爆.
    orderbook_history_buffer = OrderbookHistoryBuffer(max_age_s=15.0, max_tokens=2000)
    # DataGraph 是 4 个扁平 store 的层次化视图入口（docs/新架构方案.md §3.1）。
    # DecisionContextBuilder / API aggregators / 未来其他决策路径都从这里读，
    # 避免散落跨 store 拼接。snapshot-and-release 策略，P0 路径无长锁。
    data_graph = DataGraph(
        market_registry=registry,
        orderbook_history_buffer=orderbook_history_buffer,
        account_state_store=account_state_store,
        market_metadata_store=market_metadata_store,
    )
    # 派生指标 store + publisher: ws 推送时同步算派生指标 (microprice / depth_imbalance
    # / 滑点表 / 15s 波动 / windows delta / whale / 流动性评级), 同步写入 store.
    # P0 量化决策接到 ORDERBOOK_SNAPSHOT_UPDATED 事件时 derived 已对齐 snapshot 新鲜度;
    # admin endpoint O(1) 读 store. 实测 compute_derived ~245μs/次, 推送延迟可忽略.
    orderbook_derived_store = OrderbookDerivedStore(max_tokens=2000)
    orderbook_derived_publisher = OrderbookDerivedPublisher(
        store=orderbook_derived_store,
        history_buffer=orderbook_history_buffer,
        delta_store=orderbook_delta_store,
    )
    # market 销毁时同步清 derived cache, 跟随 market lifecycle.
    lifecycle_registry.register_prune_listener(
        "orderbook_derived_store",
        orderbook_derived_store.evict_market,
    )
    # market prune 时同步清 gamma snapshot store——市场被回收后没人会查它的
    # gamma 元数据，留在 store 里只是内存浪费。
    lifecycle_registry.register_prune_listener(
        "gamma_snapshot_store",
        lambda cid, _tokens: gamma_snapshot_store.prune(cid),
    )
    market_ws_worker = MarketWsWorker(
        event_bus=event_bus,
        registry=registry,
        rest_snapshot_loader=load_market_rest_snapshot,
        orderbook_delta_store=orderbook_delta_store,
        orderbook_history_buffer=orderbook_history_buffer,
        derived_publisher=orderbook_derived_publisher,
        # WS market_resolved 即时 prune 用: 收到 polymarket 推送的 resolved 事件
        # 后立即查账户敞口, 无敞口立即 prune (不等 reconcile 5min 周期).
        account_snapshot_provider=account_state_store.snapshot,
    )

    # execution_client 必须在 market_ws_worker 之后构造——paper 模式直接复用真实
    # Polymarket WS 推送的 orderbook（market_ws_worker.snapshot），实盘签名走真
    # trading_client。
    paper_ledger: PaperVirtualLedger | None = None
    goalserve_lazy_client: GoalserveLazyClient | None = None
    # build_runtime 末尾透传给 RuntimeComponents.background_tasks，
    # shutdown_runtime 统一 cancel + gather。
    initial_background_tasks: dict[str, asyncio.Task[None]] = {}
    if settings.paper_trading_mode:
        execution_client, paper_ledger, goalserve_lazy_client, paper_tasks = build_paper_runtime(
            settings=settings,
            account_state_store=account_state_store,
            registry=registry,
            market_ws_worker=market_ws_worker,
        )
        initial_background_tasks.update(paper_tasks)
    elif trading_client is not None:
        execution_client = PolymarketOrderExecutionClient(trading_client)
    else:
        execution_client = InMemoryPolymarketOrderClient()
    order_executor = PolymarketOrderExecutor(
        client=execution_client,
        outbox=outbox,
        thread_pool=trading_thread_pool,
        sign_timeout_ms=settings.order_sign_timeout_ms,
        submit_timeout_ms=settings.order_submit_timeout_ms,
        critical_lock_timeout_ms=settings.critical_lock_timeout_ms,
        metrics=metrics,
    )
    market_ingest_service = MarketIngestService(
        workflow=workflow,
        registry=registry,
        market_tracker=market_ws_worker,
        account_snapshot_provider=account_state_store.snapshot,
        # 300s(5min) audit 节流:discovery 每秒扫 22 个新 cid,1h 累 420 MB audit;
        # 5min 颗粒度对复盘"为什么这个市场被拒"足够,节省 80% audit 写入.
        filter_emit_min_interval_s=300.0,
    )
    decision_recorder = DecisionEventRecorder(outbox=outbox)
    decision_context_builder = DecisionContextBuilder(
        workflow=workflow,
        data_graph=data_graph,
        decision_recorder=decision_recorder,
    )
    order_gateway = OrderGateway(
        executor=order_executor,
        lifecycle_bus=lifecycle_bus,
        event_bus=event_bus,
    )
    user_ws_worker = UserWsWorker(
        event_bus=event_bus,
        account_state_store=account_state_store,
    )

    entry_metadata_for_event = build_entry_metadata_for_event_provider(
        registry=registry,
        market_metadata_store=market_metadata_store,
        workflow=workflow,
    )
    entry_metadata_for_market = build_entry_metadata_for_market_provider(
        market_metadata_store=market_metadata_store,
    )

    market_tick_worker = MarketTickWorker(
        event_bus=event_bus,
        decision_context_builder=decision_context_builder,
        order_gateway=order_gateway,
        account_state_store=account_state_store,
        portfolio_budget_usdc=settings.portfolio_budget_usdc,
        kelly_fraction=workflow_config.kelly_fraction,
        kelly_max_position_fraction=workflow_config.kelly_max_position_fraction,
        kelly_min_edge=workflow_config.kelly_min_edge,
        kelly_min_stake_usdc=workflow_config.kelly_min_stake_usdc,
        kelly_allow_round_up_to_market_min=workflow_config.kelly_allow_round_up_to_market_min,
        kelly_round_up_max_overbet_ratio=workflow_config.kelly_round_up_max_overbet_ratio,
        entry_metadata_provider=entry_metadata_for_event,
        orderbook_direction_signal_reader=orderbook_delta_store.direction_signal,
        parameter_store=parameter_store,
    )
    reconcile_service = ReconcileService(
        workflow=workflow,
        entry_metadata_provider=entry_metadata_for_market,
        orderbook_reader=decision_context_builder.lookup_orderbook,
    )
    # 注册 market_tick_worker prune listener:market prune 时同步清 worker
    # 内部 4 个 cid/token 索引 dict(_market_lifecycle / _token_*) 防内存泄漏.
    lifecycle_registry.register_prune_listener(
        "market_tick_worker",
        market_tick_worker.evict_market,
    )
    # market_metadata_store 已有 remove API,适配成 listener signature 注册:
    lifecycle_registry.register_prune_listener(
        "market_metadata_store",
        lambda cid, _tokens: market_metadata_store.remove(condition_id=cid),
    )
    reconcile_worker = ReconcileWorker(
        event_bus=event_bus,
        reconcile_service=reconcile_service,
        registry_snapshot_provider=registry.snapshot,
        account_snapshot_provider=account_state_store.snapshot,
        account_state_store=account_state_store,
        order_gateway=order_gateway,
        registry=registry,
        market_ws_worker=market_ws_worker,
        gamma_client=gamma_client,
        clob_client=clob_client,
        data_client=data_client,
        trading_client=trading_client,
        lifecycle_bus=lifecycle_bus,
        paper_mode=settings.paper_trading_mode,
        gamma_snapshot_store=gamma_snapshot_store,
        # 用户态拉取已经交给独立 UserAccountPoller，reconcile 主循环不再内联 fetch
        # → 避免 data API / clob balance 网络抖动牵连 market 元数据刷新链路。
        refresh_account_inline=False,
    )
    # 注册 authority_refresher prune listener:market prune 时清 _condition_failure_counts.
    lifecycle_registry.register_prune_listener(
        "reconcile_authority_refresher",
        reconcile_worker._authority_refresher.evict_market,
    )
    market_discovery_worker = MarketDiscoveryWorker(
        market_ingest_service=market_ingest_service,
        event_bus=event_bus,
        retry_delay_seconds=MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS,
    )
    market_discovery_scan = FullMarketDiscoveryState()
    (
        live_state_store,
        live_source_registry,
        live_source_match_service,
        live_source_lifecycle_binder,
        live_source_feeders,
        live_source_closers,
    ) = _build_live_source_components(
        settings,
        registry=registry,
        market_metadata_store=market_metadata_store,
        event_bus=event_bus,
        workflow=workflow,
    )
    live_source_match_service.attach()
    # 接入 LifecycleRegistry：market added 时立即 subscribe（C 模式事件驱动），
    # prune 时立即 unsubscribe_all。reconcile_subscriptions 仍由 scheduler 2s
    # + discovery 完成 hook 周期触发作自愈兜底。
    live_source_lifecycle_binder.bind_to_lifecycle_registry(lifecycle_registry)
    season_state_store = SeasonStateStore()
    bind_runtime_season_state(runtime_ports, season_state_store)
    # 注：ESPN-based season-state + series-state worker 已经删除（只服务传统
    # 体育，对当前 e-sports 100% 浪费）。store 保留，strategy ports 仍可绑，
    # 没有 writer 等于空 store——strategy 读到 None 时门控自然跳过。
    season_odds_worker, season_odds_client = _build_season_odds_worker(
        settings,
        registry=registry,
        market_metadata_store=market_metadata_store,
        workflow=workflow,
        event_bus=event_bus,
    )
    game_odds_worker, game_odds_client = _build_game_odds_worker(
        settings,
        registry=registry,
        market_metadata_store=market_metadata_store,
        workflow=workflow,
        event_bus=event_bus,
    )
    # odds workers + user_ws 的 prune listeners
    if season_odds_worker is not None:
        lifecycle_registry.register_prune_listener(
            "season_odds_worker",
            season_odds_worker.evict_market,
        )
    if game_odds_worker is not None:
        lifecycle_registry.register_prune_listener(
            "game_odds_worker",
            game_odds_worker.evict_market,
        )
    lifecycle_registry.register_prune_listener("user_ws_worker", user_ws_worker.evict_market)
    # account_state 的 _fills 也按 market lifecycle 清(fills 已写 DB,内存不必常驻 dead market).
    lifecycle_registry.register_prune_listener(
        "account_state_store",
        account_state_store.evict_market,
    )
    pregame_worker, pregame_client = _build_pregame_worker(
        settings,
        registry=registry,
        market_metadata_store=market_metadata_store,
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
        sse_snapshot_provider=sse_subscription_registry.snapshot,
        trading_queue_warn_depth=settings.trading_queue_warn_depth,
        entry_signal_to_submit_warn_ms=settings.entry_signal_to_submit_warn_ms,
        outbox_depth_warn=settings.persistence_event_queue_max_size,
        reconcile_stale_after_seconds=max(settings.market_sync_interval_seconds * 2, 60),
    )
    # N13：把 supervisor heartbeat 接到 trading_decision worker；worker 内部不直接持有
    # supervisor 实例，避免 P0 worker 反向耦合 runtime/状态层。
    market_tick_worker.bind_heartbeat(
        lambda **kwargs: supervisor.heartbeat_worker("trading_decision", **kwargs)
    )
    # ObservabilityBridge——周期把组件 stats 投影到 MetricsRegistry gauge。
    # 13 个 gauge 一次性 wire（audit dedupe / garbage / live source / account）。
    observability_bridge = ObservabilityBridge(
        metrics=metrics,
        audit_deduper=audit_deduper,
        garbage_filter=audit_garbage_filter,
        live_state_store=live_state_store,
        live_source_registry=live_source_registry,
        account_state_store=account_state_store,
    )
    # AdminWsPublisher——event_bus 增量推送骨架。startup 时 .start() 启动 drain
    # task；shutdown 时 await .stop()。endpoint 接入待 stream.py 改造。
    admin_ws_publisher = AdminWsPublisher(event_bus=event_bus)
    return RuntimeComponents(
        settings=settings,
        readiness=readiness,
        workflow=workflow,
        logging_runtime=logging_runtime,
        parameter_store=parameter_store,
        gamma_client=gamma_client,
        clob_client=clob_client,
        data_client=data_client,
        trading_client=trading_client,
        polymarket_ws_client=polymarket_ws_client,
        event_bus=event_bus,
        registry=registry,
        lifecycle_registry=lifecycle_registry,
        gamma_snapshot_store=gamma_snapshot_store,
        outbox=outbox,
        db_engine=db_engine,
        db_session_factory=db_session_factory,
        persistence_repository=persistence_repository,
        persistence_worker=persistence_worker,
        audit_garbage_filter=audit_garbage_filter,
        audit_deduper=audit_deduper,
        observability_bridge=observability_bridge,
        admin_ws_publisher=admin_ws_publisher,
        account_state_store=account_state_store,
        market_metadata_store=market_metadata_store,
        order_executor=order_executor,
        market_ws_worker=market_ws_worker,
        orderbook_delta_store=orderbook_delta_store,
        orderbook_history_buffer=orderbook_history_buffer,
        data_graph=data_graph,
        orderbook_derived_store=orderbook_derived_store,
        orderbook_derived_publisher=orderbook_derived_publisher,
        user_ws_worker=user_ws_worker,
        market_ingest_service=market_ingest_service,
        market_discovery_worker=market_discovery_worker,
        market_discovery_scan=market_discovery_scan,
        live_state_store=live_state_store,
        live_source_registry=live_source_registry,
        live_source_match_service=live_source_match_service,
        live_source_lifecycle_binder=live_source_lifecycle_binder,
        live_source_feeders=live_source_feeders,
        live_source_closers=live_source_closers,
        season_state_store=season_state_store,
        season_odds_worker=season_odds_worker,
        season_odds_client=season_odds_client,
        game_odds_worker=game_odds_worker,
        game_odds_client=game_odds_client,
        pregame_worker=pregame_worker,
        pregame_client=pregame_client,
        decision_context_builder=decision_context_builder,
        order_gateway=order_gateway,
        market_tick_worker=market_tick_worker,
        reconcile_service=reconcile_service,
        reconcile_worker=reconcile_worker,
        scheduler=scheduler,
        supervisor=supervisor,
        metrics=metrics,
        trading_thread_pool=trading_thread_pool,
        maintenance_thread_pool=maintenance_thread_pool,
        maintenance_process_pool=maintenance_process_pool,
        sse_subscription_registry=sse_subscription_registry,
        paper_ledger=paper_ledger if settings.paper_trading_mode else None,
        goalserve_lazy_client=goalserve_lazy_client,
        background_tasks=initial_background_tasks,
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
    _perf = SystemPerfMonitor.get()
    _perf.start_phase("bootstrap_total")

    runtime.supervisor.set_phase(RuntimePhase.CONFIG_LOADING)
    _perf.start_phase("register_workers")
    _register_runtime_workers(runtime)
    _perf.end_phase("register_workers")
    _perf.start_phase("seed_metrics")
    _seed_default_metrics(runtime)
    _sync_runtime_metrics(runtime)
    _perf.end_phase("seed_metrics")

    _perf.start_phase("db_check")
    db_ready = await _check_database_connection(runtime.db_session_factory)
    _perf.end_phase("db_check")
    runtime.supervisor.mark_db_ready(db_ready, reason="database_unavailable" if not db_ready else "")
    runtime.supervisor.mark_trading_client_ready(
        runtime.trading_client is not None and runtime.readiness.ready_to_trade,
        reason="trading_client_unavailable",
    )

    runtime.supervisor.set_phase(RuntimePhase.INFRA_READY)
    runtime.supervisor.set_phase(RuntimePhase.RECOVERING_SNAPSHOT)
    _perf.start_phase("load_reference_state")
    loaded_reference = await _load_reference_state(runtime)
    _perf.end_phase("load_reference_state")

    # 之前 lifespan 同步 await _run_reconcile_once → 9 个 orphan account-exposure
    # 仓位每个要 gamma+clob 多查,实测 10-11s 全在这里。lifespan 阻塞 → health
    # 不响应、wait_for_http 临界超时。改为后台任务:lifespan 几秒完成,reconcile
    # 在 background 跑,完成后 supervisor 状态自然过渡到 trading_enabled。
    # CLAUDE.md §7 也明确"reconciler 不长时间持锁阻塞 P0 路径",同精神适用启动。
    runtime.supervisor.set_phase(RuntimePhase.RECONCILING)

    async def _async_startup_reconcile() -> None:
        try:
            result = await _run_reconcile_once(
                runtime,
                source="startup",
                refresh_market_authority=False,
            )
            runtime.bootstrap_summary["startup_reconcile"] = {
                "trace_id": result.trace_id,
                "markets": len(result.plan.market_plans),
                "diff_count": result.plan.diff_count,
                "applied_actions": len(result.applied_actions),
                "failed_actions": len(result.failed_actions),
            }
            # reconcile 跑完刷新 supervisor,让 phase 过渡到 trading_enabled。
            snapshot = await runtime.supervisor.refresh()
            runtime.metrics.set_trading_gate(
                snapshot.automatic_trading_enabled,
                reason=trading_gate_reason(snapshot),
                source="supervisor",
            )
        except Exception as exc:  # pragma: no cover - startup may fail on live deps
            runtime.bootstrap_summary["startup_reconcile"] = {"error": str(exc)}
            runtime.supervisor.mark_degraded(f"startup_reconcile_failed:{exc}")
            logger.warning(
                "startup reconcile failed; runtime remains degraded",
                extra={"reason": str(exc)},
            )

    runtime.background_tasks["startup_reconcile"] = asyncio.create_task(
        _async_startup_reconcile(), name="startup_reconcile"
    )
    reconcile_summary: dict[str, Any] = {"status": "scheduled_background"}

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
    # 启动期合理性告警：交易客户端已就绪但直播状态 worker 关闭——意味着 funnel 上游
    # 永远拿不到 candidates，运维容易误以为"策略选择性谨慎"。明确告警让人在 .env
    # 里打开 SPORTS_LIVE_STATE_ENABLED 或确认是有意关闭。
    runtime_settings = runtime.settings
    wallet_secret = runtime_settings.wallet_private_key
    wallet_present = wallet_secret is not None and bool(wallet_secret.get_secret_value())
    funded = runtime_settings.portfolio_budget_usdc > Decimal("0")
    if not runtime_settings.sports_live_state_enabled and wallet_present and funded:
        logger.warning(
            "sports_live_state worker disabled while trading config is funded — "
            "strategy entry-signals depend on live state; funnel will stay at 0 candidates "
            "until SPORTS_LIVE_STATE_ENABLED=true",
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

    # AdminWsPublisher —— 取消 drain task + unregister event_bus listener
    with suppress(Exception):
        await runtime.admin_ws_publisher.stop()
    with suppress(Exception):
        await runtime.order_gateway.aclose()
    with suppress(Exception):
        await runtime.order_executor.aclose()
    for client in (runtime.gamma_client, runtime.clob_client, runtime.data_client):
        with suppress(Exception):
            await client.aclose()
    # 停 live_source feeders 自身的 wrapper task（client 内部 per-sport task 由
    # 下面的 live_source_closers 单独清理）
    for feeder in runtime.live_source_feeders:
        with suppress(Exception):
            await feeder.stop()
    for closer in runtime.live_source_closers:
        with suppress(Exception):
            await closer()
    if runtime.season_odds_client is not None:
        with suppress(Exception):
            await runtime.season_odds_client.aclose()
    if runtime.game_odds_client is not None:
        with suppress(Exception):
            await runtime.game_odds_client.aclose()
    if runtime.pregame_client is not None:
        with suppress(Exception):
            await runtime.pregame_client.aclose()
    with suppress(Exception):
        await runtime.db_engine.dispose()
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
    runtime.supervisor.register_worker("operator_api", priority="P3", state=WorkerLifecycleState.RUNNING)
    runtime.supervisor.register_worker("market_discovery", priority="P2")
    runtime.supervisor.register_worker("market_ws", priority="P0", state=WorkerLifecycleState.PAUSED)
    runtime.supervisor.register_worker("user_ws", priority="P0", state=WorkerLifecycleState.PAUSED)
    runtime.supervisor.register_worker("trading_decision", priority="P0")
    runtime.supervisor.register_worker("reconcile", priority="P2")
    for feeder in runtime.live_source_feeders:
        # feeder.name 格式 "live-source-feeder:goalserve_inplay"，转 supervisor key
        worker_name = feeder.name.replace("live-source-feeder:", "live_source_feeder_")
        runtime.supervisor.register_worker(worker_name, priority="P2")
    runtime.supervisor.register_worker("live_source_reconcile", priority="P2", state=WorkerLifecycleState.RUNNING, detail="scheduler-driven; first run after interval")
    if runtime.season_odds_worker is not None:
        runtime.supervisor.register_worker("sports_season_odds_sync", priority="P2")
    if runtime.game_odds_worker is not None:
        runtime.supervisor.register_worker("sports_game_odds_sync", priority="P2")
    if runtime.pregame_worker is not None:
        runtime.supervisor.register_worker("sports_pregame_odds_sync", priority="P2")
    runtime.supervisor.register_worker("persistence", priority="P3")
    runtime.supervisor.register_worker("audit_retention_purge", priority="P3", state=WorkerLifecycleState.RUNNING, detail="scheduler-driven; first run after interval")
    runtime.supervisor.register_worker("dead_records_purge", priority="P3", state=WorkerLifecycleState.RUNNING, detail="scheduler-driven; first run after interval")


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
        # 预热连接池:并发跑 N 次 SELECT 1,迫使 pool 建立 N 个 conn,避免冷调用 200ms+ 抖动
        await _warmup_db_pool(session_factory, n=5)
        return True
    except Exception as exc:  # pragma: no cover - depends on external db
        logger.warning("database readiness check failed", extra={"reason": str(exc)})
        return False


async def _warmup_db_pool(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    n: int = 5,
) -> None:
    """并发 borrow N 个 session 跑 SELECT 1。

    SQLAlchemy AsyncEngine 默认 lazy 建连——首次 query 会触发 DNS/TCP/SSL/auth
    ~200ms。启动时预热 N 个 conn 进 pool,后续 query 直接复用,p99 抖动收敛.
    """
    async def _one() -> None:
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("db pool warmup conn failed: %s", exc)
    await asyncio.gather(*[_one() for _ in range(n)], return_exceptions=True)


# _restore_account_reference_state / _restore_trackable_markets 已删：
# 启动期不再从 DB 恢复任何运行时状态，完全靠 Polymarket 官方 API 拉权威值
# （reconcile 拉 balance/positions/orders，discovery 拉 markets）。


async def _handle_market_ws_message(runtime, message) -> None:
    await handle_market_ws_message(runtime, message)


async def _load_reference_state(runtime: RuntimeComponents) -> dict[str, int]:
    # §3 强化版：启动期 **不读 DB**。所有运行时真相（balance / allowance / positions /
    # orders / fills / markets）都从 Polymarket 官方 API 拉取——reconcile 第一轮
    # (秒级) 内会覆盖。DB 仅审计，不做运行时数据源/启动 warmup。
    #
    # 启动到 reconcile 完成之间的空白窗口（balance=0、positions=()）由
    # readiness 门控保护：exit overlay / decide_exit 检查 last_reconcile_at=None
    # 即 skip；自动交易 phase 要求 reconcile_fresh=True 才进入 trading_enabled。
    # 所以"零状态"窗口安全，不会误判挂单或决策。
    return {"markets": 0, "positions": 0, "open_orders": 0, "fills": 0, "account_snapshots": 0}


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
            runner=runtime.market_tick_worker.run,
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
    # AdminWsPublisher —— 启动 drain task + 注册 event_bus listener；
    # endpoint 接入待 stream.py 改造，目前订阅集为空 → drain 内 push 为空操作
    runtime.admin_ws_publisher.start()
    # live_source feeder × N（每 provider 一个）：feeder 内部 create_task 自管，
    # 这里只 start。stop 在 shutdown_runtime 中调 feeder.stop() 与 goalserve
    # client.aclose 配套清理。supervisor heartbeat 由 feeder 自身打？暂无——
    # 健康可观测靠 LiveStateStore.health/last_observed_at（Step 7 横切层接 metric）。
    for feeder in runtime.live_source_feeders:
        feeder.start()


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
    # P1 not P2: 死循环 bug — P2 在 p0_backpressure 触发后被 pause_low_priority 暂停,
    # 但 reconcile stale 又会让 readiness fail/phase=degraded 永远无法 clear(periodic_reconcile
    # 是清除 reconcile_not_fresh blocking_reason 的唯一来源)。reconcile 是恢复路径,
    # 不应该被 backpressure 拖死。
    runtime.scheduler.register_job(
        "periodic_reconcile",
        lambda: _publish_reconcile_trigger(runtime, source="scheduled"),
        priority="P1",
        interval_seconds=float(runtime.settings.market_sync_interval_seconds),
        tags=("reconcile",),
        start=True,
        run_immediately=False,
    )
    # 持仓周期评估：5s 一次主动 trigger decide_exit，覆盖薄盘场景（orderbook
    # 长期不更新但比赛/赔率/math_lock 仍在变）。复用 _handle_orderbook_snapshot_updated
    # 路径包括 MTM 刷新 + 多信号投票止损/止盈 reprice。
    runtime.scheduler.register_job(
        "position_heartbeat",
        lambda: _publish_position_heartbeat_tick(runtime),
        priority="P1",
        interval_seconds=5.0,
        tags=("position", "exit"),
        start=True,
        run_immediately=False,
    )
    # live_source reconcile（demand-driven 订阅校准）：discovery_runner 完成一轮
    # 会立即调一次 binder.reconcile_subscriptions() 作为主触发；此 scheduler job
    # 作为周期兜底，处理"市场状态变化（如 game_start_time 到 → active_predicate
    # 翻转）但没新 market 进入"的场景。2s 与 discovery cadence 同步。
    runtime.scheduler.register_job(
        "live_source_reconcile",
        lambda: _run_live_source_reconcile(runtime),
        priority="P2",
        interval_seconds=2.0,
        tags=("live_source", "reconcile"),
        start=True,
        run_immediately=True,
    )
    # ObservabilityBridge —— 5s 周期把组件 stats 投到 metrics gauge（§11.2 表的批量
    # 落地路径，无需改业务路径）
    runtime.scheduler.register_job(
        "observability_bridge_sync",
        runtime.observability_bridge.sync,
        priority="P3",
        interval_seconds=5.0,
        tags=("observability", "bridge"),
        start=True,
        run_immediately=False,
    )
    if runtime.season_odds_worker is not None:
        runtime.scheduler.register_job(
            "sports_season_odds_sync",
            lambda: _run_sports_season_odds_sync(runtime),
            priority="P2",
            interval_seconds=float(runtime.settings.sports_season_odds_interval_seconds),
            tags=("sports_season_odds",),
            start=True,
            run_immediately=True,
        )
    if runtime.game_odds_worker is not None:
        runtime.scheduler.register_job(
            "sports_game_odds_sync",
            lambda: _run_sports_game_odds_sync(runtime),
            priority="P2",
            interval_seconds=float(runtime.settings.sports_game_odds_interval_seconds),
            tags=("sports_game_odds",),
            start=True,
            run_immediately=True,
        )
    if runtime.pregame_worker is not None:
        runtime.scheduler.register_job(
            "sports_pregame_odds_sync",
            lambda: _run_sports_pregame_odds_sync(runtime),
            priority="P2",
            interval_seconds=float(runtime.settings.goalserve_pregame_interval_seconds),
            tags=("sports_pregame_odds",),
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
    # 用户态独立后台轮询：data API positions + clob balance + open_orders + fills。
    # 不再在 reconcile 主循环内联——避免用户态网络抖动牵连 market 元数据刷新链路。
    # 与 reconcile 同 cadence（market_sync_interval_seconds，默认 20s），但独立失败。
    # paper 模式下 refresh_account 自身 paper_mode 早退（paper_balance_syncer 是
    # 唯一权威），所以 poller 调到也 no-op，不抢 store。
    runtime.scheduler.register_job(
        "user_account_poll",
        lambda: runtime.reconcile_worker._authority_refresher.refresh_account(
            trace_id=f"user-account-poll-{uuid4().hex}",
            markets=(),
        ),
        priority="P2",
        interval_seconds=float(runtime.settings.market_sync_interval_seconds),
        tags=("user_account", "data_api", "clob_balance"),
        start=True,
        run_immediately=True,
    )
    # 60s 写一次账户净值快照——append-only 时间序列，喂给 equity-curve 审计接口。
    # 任意 DB 异常都不能反向阻塞交易主链路，所以失败只记日志、不抛 supervisor 错。
    runtime.scheduler.register_job(
        "account_snapshot_recorder",
        lambda: _record_account_snapshot(runtime),
        priority="P3",
        interval_seconds=60.0,
        tags=("portfolio", "persistence"),
        start=True,
        run_immediately=False,
    )
    # 5 分钟扫一次已结算市场——把 outcomePrices 抓回来发 MARKET_SETTLED 事件，
    # 给 calibration / Brier 提供 ground truth。失败/未结算/查不到一律静默。
    runtime.scheduler.register_job(
        "settlement_scanner",
        lambda: _run_settlement_scan(runtime),
        priority="P3",
        interval_seconds=300.0,
        tags=("settlement", "calibration"),
        start=True,
        run_immediately=False,
    )
    # audit_events 保留期清理——每天跑一次 DELETE WHERE created_at < cutoff，
    # 让 audit 表稳态在 retention_days 内的数据量。retention_days=0 时仍注册
    # job，但 purge_audit_events_once 会直接 skipped 不实际删除（运维显式关闭语义）。
    runtime.scheduler.register_job(
        "audit_retention_purge",
        lambda: _run_audit_retention_purge(runtime),
        priority="P3",
        interval_seconds=float(runtime.settings.audit_retention_interval_seconds),
        tags=("audit", "retention", "persistence"),
        start=True,
        run_immediately=True,
    )
    # 死记录(终态 orders / 已结算空 positions / 老 fills)清理 — 每天跑一次,
    # 防止 reconcile 用历史 fills/orders 反复推无意义 market refs 拖慢启动。
    runtime.scheduler.register_job(
        "dead_records_purge",
        lambda: _run_dead_records_purge(runtime),
        priority="P3",
        interval_seconds=float(runtime.settings.dead_records_retention_interval_seconds),
        tags=("dead_records", "retention", "persistence"),
        start=True,
        run_immediately=True,
    )


async def _run_supervised_loop(
    runtime: RuntimeComponents,
    *,
    name: str,
    runner: Callable[[], Awaitable[None]],
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


async def _publish_position_heartbeat_tick(runtime: RuntimeComponents) -> None:
    """周期性合成 ORDERBOOK_SNAPSHOT_UPDATED 事件 trigger 持仓评估。

    薄盘场景：orderbook 可能数十秒没新 push（无人挂单），订阅触发的 reprice
    路径完全静默。但比赛比分/Goalserve 赔率/math_lock 仍在变——必须主动 trigger
    decide_exit 才能基于最新非盘口数据决策止损/止盈。

    5s 周期：每个 token 合成一个 event publish 到 trading queue，复用现有
    _handle_orderbook_snapshot_updated 路径（包括 MTM 刷新 + reprice）。
    """

    snapshot = runtime.account_state_store.snapshot()
    if snapshot is None:
        return
    for position in snapshot.positions:
        if position.shares <= Decimal("0"):
            continue
        await runtime.event_bus.publish(
            OutboxPriority.P2,
            DomainEvent(
                trace_id=_runtime_trace_id("position-tick", source=position.condition_id),
                event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
                event_id=uuid4().hex,
                condition_id=position.condition_id,
                token_id=position.token_id,
                reason="position_heartbeat_tick",
                payload={"source": "position_heartbeat", "synthetic": True},
            ),
        )


async def _publish_reconcile_trigger(
    runtime: RuntimeComponents,
    *,
    source: str,
) -> None:
    await runtime.event_bus.publish(
        OutboxPriority.P2,
        DomainEvent(
            trace_id=_runtime_trace_id("reconcile-trigger", source=source),
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


def _runtime_trace_id(prefix: str, *, source: str | None = None) -> str:
    """生成能落入持久化 trace_id 字段的运行时 trace。

    source 仍通过事件 reason、payload 和 supervisor detail 保留；trace_id 只承担
    关联用途，不能把完整 source 拼进去导致审计写库失败。
    """

    safe_prefix = "".join(ch if ch.isalnum() else "-" for ch in prefix.strip().lower()).strip("-")
    safe_prefix = safe_prefix or "trace"
    if source:
        source_digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
        safe_prefix = f"{safe_prefix[:22]}-{source_digest}"
    return f"{safe_prefix[:31]}-{uuid4().hex}"


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
        condition_id = event.condition_id
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
    max_batch_size = max(1, min(runtime.settings.maintenance_event_queue_max_size, _RECONCILE_BATCH_SIZE_LIMIT))
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
) -> ReconcileWorkerResult:
    runtime.supervisor.heartbeat_worker("reconcile", detail=source)
    started_at = asyncio.get_running_loop().time()
    try:
        result = await runtime.reconcile_worker.reconcile_once(
            trace_id=_runtime_trace_id("reconcile", source=source),
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


async def _run_live_source_reconcile(runtime: RuntimeComponents) -> None:
    """周期性 reconcile_subscriptions 兜底——补 discovery 主路径漏触发的订阅变化。"""

    runtime.supervisor.heartbeat_worker("live_source_reconcile", detail="syncing")
    try:
        result = runtime.live_source_lifecycle_binder.reconcile_subscriptions()
    except Exception as exc:
        runtime.supervisor.mark_worker_error(
            "live_source_reconcile",
            detail="reconcile_failed",
            last_error=str(exc),
        )
        _sync_runtime_metrics(runtime)
        raise
    runtime.supervisor.heartbeat_worker(
        "live_source_reconcile",
        detail=f"added={result['added']} removed={result['removed']} errors={result['errors']}",
    )
    _sync_runtime_metrics(runtime)


async def _run_sports_season_odds_sync(runtime: RuntimeComponents) -> None:
    worker = runtime.season_odds_worker
    if worker is None:
        return
    runtime.supervisor.heartbeat_worker("sports_season_odds_sync", detail="syncing")
    try:
        refreshed = await worker.sync_once()
    except Exception as exc:
        runtime.supervisor.mark_worker_error(
            "sports_season_odds_sync",
            detail="sync_failed",
            last_error=str(exc),
        )
        raise
    runtime.supervisor.heartbeat_worker(
        "sports_season_odds_sync",
        detail=f"refreshed={refreshed}",
    )


async def _run_sports_game_odds_sync(runtime: RuntimeComponents) -> None:
    worker = runtime.game_odds_worker
    if worker is None:
        return
    runtime.supervisor.heartbeat_worker("sports_game_odds_sync", detail="syncing")
    try:
        refreshed = await worker.sync_once()
    except Exception as exc:
        runtime.supervisor.mark_worker_error(
            "sports_game_odds_sync",
            detail="sync_failed",
            last_error=str(exc),
        )
        raise
    runtime.supervisor.heartbeat_worker(
        "sports_game_odds_sync",
        detail=f"refreshed={refreshed}",
    )


async def _run_sports_pregame_odds_sync(runtime: RuntimeComponents) -> None:
    worker = runtime.pregame_worker
    if worker is None:
        return
    runtime.supervisor.heartbeat_worker("sports_pregame_odds_sync", detail="syncing")
    try:
        matches = await worker.sync_once()
    except Exception as exc:
        runtime.supervisor.mark_worker_error(
            "sports_pregame_odds_sync",
            detail="sync_failed",
            last_error=str(exc),
        )
        raise
    runtime.supervisor.heartbeat_worker(
        "sports_pregame_odds_sync",
        detail=f"matches={matches}",
    )


async def _record_account_snapshot(runtime: RuntimeComponents) -> None:
    """把当前内存账户状态写一行到 ``account_snapshots`` 时间序列。

    严格异步、非交易主链路：DB 异常只记日志，绝不向 supervisor 反传错误，
    避免审计写库回灌阻塞 P0。``net_value_usdc`` 在写入时根据当前持仓
    ``current_value`` 一次性算好，下游 ``equity-curve`` 不再依赖历史 mark。
    """

    account_state = runtime.account_state_store
    snapshot = account_state.snapshot()
    try:
        async with runtime.db_session_factory() as session:
            await AccountSnapshotRepository(session).save_snapshot(snapshot)
            await session.commit()
    except Exception as exc:  # pragma: no cover - depends on external db
        logger.warning(
            "account snapshot recorder failed",
            extra={"reason": str(exc)},
        )


async def _run_dead_records_purge(runtime: RuntimeComponents) -> None:
    """每天跑一次,按 condition_id 关联清死市场的 orders/fills/positions/
    audit/outbox 全部 trail,加时间兜底清无 cid 事件 + account_snapshots。"""

    summary = await purge_dead_records_once(
        runtime.db_session_factory,
        dead_records_retention_days=runtime.settings.dead_records_retention_days,
        fills_retention_days=runtime.settings.fills_retention_days,
        outbox_retention_days=runtime.settings.outbox_retention_days,
        decision_records_retention_days=runtime.settings.decision_records_retention_days,
        account_snapshots_retention_days=runtime.settings.account_snapshots_retention_days,
    )
    runtime.supervisor.heartbeat_worker(
        "dead_records_purge",
        detail=(
            f"orders={summary.get('deleted_orders', 0)} "
            f"fills={summary.get('deleted_fills', 0)} "
            f"positions={summary.get('deleted_positions', 0)} "
            f"audit={summary.get('deleted_audit_events', 0)} "
            f"outbox={summary.get('deleted_outbox_events', 0)} "
            f"decisions={summary.get('deleted_decision_records', 0)} "
            f"snapshots={summary.get('deleted_account_snapshots', 0)} "
            f"err={'!' if summary.get('error') else '-'}"
        ),
    )


async def _run_audit_retention_purge(runtime: RuntimeComponents) -> None:
    """每天清理一次 audit_events 表中超出保留期的行。

    P3 后台任务——所有 DB 异常都在 purge_audit_events_once 内部 catch + 日志。
    """

    # §13.5 分层 retention：trade 30d / 拒绝 7d / heartbeat 1d / fills 永久。
    # 旧 settings.audit_retention_days 仍可用作 fallback_days（覆盖未列入 tier
    # 的 event_title），但分层 tier 用 DEFAULT_AUDIT_RETENTION_POLICY 集中管理。
    from polymarket_trader.infra.db.retention_policy import (
        DEFAULT_AUDIT_RETENTION_POLICY,
        AuditRetentionPolicy,
    )
    fallback_days = runtime.settings.audit_retention_days
    if fallback_days <= 0:
        # 配置为 0 或负 → 完全禁用 purge（保持旧行为）
        runtime.supervisor.heartbeat_worker(
            "audit_retention_purge",
            detail="disabled (audit_retention_days <= 0)",
        )
        return
    policy = AuditRetentionPolicy(
        tiers=DEFAULT_AUDIT_RETENTION_POLICY.tiers,
        fallback_days=fallback_days,
    )
    summary = await purge_audit_events_once(
        runtime.db_session_factory,
        batch_size=runtime.settings.audit_retention_purge_batch_size,
        policy=policy,
    )
    tier_breakdown = ",".join(
        f"{t['days']}d:{t['deleted_rows']}" for t in summary.get("tiers", ())
    )
    runtime.supervisor.heartbeat_worker(
        "audit_retention_purge",
        detail=(
            f"deleted={summary['deleted_rows']} batches={summary['batches']} "
            f"tiers=[{tier_breakdown}] err={'!' if summary['error'] else '-'}"
        ),
    )


async def _run_settlement_scan(runtime: RuntimeComponents) -> None:
    """运行一次结算扫描。任何异常仅记日志——不阻塞 supervisor。

    §3 强化版：scanner 不访问 DB（DB 仅审计）。
    持仓来源 = AccountStateStore 内存快照（reconcile 已经从 Polymarket data API
    拉到内存）；幂等去重靠 service 内 in-process 集合 + outbox event_id
    (settlement:{cid}) 兜底，不再查 audit_events 表。
    """

    async def _gamma_by_condition(condition_id: str) -> Any | None:
        """统一通过 GammaClient.get_market_by_condition_id 反查。Gamma /markets/{id}
        端点只认内部数值 id，不接受 condition_id（否则 422）——封装在 client 里避免
        各调用方各自重复 condition_ids 过滤拼接。"""

        try:
            return await runtime.gamma_client.get_market_by_condition_id(
                condition_id, timeout_s=2.0
            )
        except Exception:
            logger.info(
                "settlement_scanner.gamma_filter_failed",
                extra={"condition_id": condition_id},
                exc_info=True,
            )
            return None

    # service 跨 tick 复用——保留 _published_settlements 集合，避免每 5 分钟
    # 对同一 condition 反复发 MARKET_SETTLED（虽然 outbox 的 event_id UNIQUE
    # 约束会兜底 dedupe，但浪费 gamma 调用）。
    service = getattr(runtime, "_settlement_scanner_service", None)
    if service is None:
        service = SettlementScannerService(
            gamma_market_by_condition=_gamma_by_condition,
            positions_provider=lambda: runtime.account_state_store.snapshot().positions,
            event_bus=runtime.event_bus,
            account_state_store=runtime.account_state_store,
        )
        runtime._settlement_scanner_service = service  # type: ignore[attr-defined]
    try:
        result = await service.run_once()
    except Exception:  # pragma: no cover - 安全网；service 内层已 catch
        logger.warning("settlement_scanner.run_failed", exc_info=True)
        return
    if result.detected or result.failed_lookups:
        logger.info(
            "settlement_scanner.tick",
            extra={
                "scanned": result.scanned,
                "skipped_already_settled": result.skipped_already_settled,
                "detected": result.detected,
                "failed_lookups": result.failed_lookups,
            },
        )


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
