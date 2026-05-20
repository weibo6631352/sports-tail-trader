from __future__ import annotations

import asyncio
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.app.admin_service import AdminService

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from polymarket_trader.api.routes.stream import SseSubscriptionRegistry
from polymarket_trader.app.market_service import MarketService
from polymarket_trader.app.ports import bind_extension_orderbook_reader, bind_extension_season_state, build_extension_ports
from polymarket_trader.app.reconcile_service import ReconcileService
from polymarket_trader.app.extension_host import load_extension
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.config import ConfigIssue, ConfigLoadError, Settings, StartupReadiness, load_settings
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.allocation import current_exposure_usdc
from polymarket_trader.domain.events import AuditEvent, DomainEvent, OutboxPriority
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.market import Market
from polymarket_trader.infra.db import (
    AccountSnapshotRepository,
    AuditEventRepository,
    DatabasePersistenceRepository,
    FillRepository,
    MarketRepository,
    OrderRepository,
    PositionRepository,
    RepositoryPage,
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
from polymarket_trader.infra.sports import (
    GoalserveClient,
    GoalserveLivescoreClient,
    GoalservePregameOddsClient,
    SeasonOddsClient,
    SportsLiveAggregateClient,
    TheOddsApiClient,
)
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
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.discovery_runner import (
    FullMarketDiscoveryState,
    MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS,
    MARKET_DISCOVERY_TICK_SECONDS,
    run_market_discovery_scan,
)
from polymarket_trader.app.decision_recorder import DecisionEventRecorder
from polymarket_trader.app.parameter_store import ParameterStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.lifecycle_bus import InProcessLifecycleBus
from polymarket_trader.runtime.metrics_sync import sync_runtime_metrics as _sync_runtime_metrics
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.runtime.ws_loops import (
    handle_market_ws_message,
    run_market_ws as _run_market_ws,
    run_user_ws as _run_user_ws,
)
from polymarket_trader.extension_api import BusinessExtension, resolve_kelly_params
from polymarket_trader.extension_api.manifest import ConfigValidator
from polymarket_trader.workers.market_discovery_worker import MarketDiscoveryWorker
from polymarket_trader.workers.market_ws import MarketWsWorker
from polymarket_trader.workers.persistence import PersistenceWorker
from polymarket_trader.workers.reconcile import ReconcileWorker, ReconcileWorkerResult
from polymarket_trader.workers.sports_live_state_worker import SportsLiveStateWorker
from polymarket_trader.workers.sports_season_odds_worker import SportsSeasonOddsWorker
from polymarket_trader.workers.sports_season_state_worker import SportsSeasonStateWorker
from polymarket_trader.workers.series_state_worker import SeriesStateWorker
from polymarket_trader.workers.game_odds_worker import GameOddsWorker
from polymarket_trader.workers.goalserve_pregame_worker import GoalservePregameWorker
from polymarket_trader.workers.trading_decision import TradingDecisionWorker
from polymarket_trader.extension_api.hooks import MarketClassificationHooks
from polymarket_trader.workers.user_ws import UserWsWorker
from polymarket_trader.infra.sports.espn_standings_client import EspnStandingsClient
from polymarket_trader.infra.sports.series_state_client import (
    EspnSeriesStateClient,
    SeriesStateClient,
)
from polymarket_trader.infra.sports.game_odds_client import (
    GameOddsClient,
    TheOddsApiGameOddsClient,
)

logger = logging.getLogger(__name__)

# 聚合器超时 = 单次 provider 超时 × 倍率，floor 防止 timeout_s 配置极小时完全没预算。
_PROVIDER_TIMEOUT_MULTIPLIER = 3
_PROVIDER_TIMEOUT_FLOOR_S = 12.0
# 启动快照加载上限，限制重启时的内存占用；超出部分等 WS 增量补齐。
_STARTUP_SNAPSHOT_ITEM_LIMIT = 500
# reconcile 批次上限，防止 maintenance 队列积压时单批过大阻塞主循环。
_RECONCILE_BATCH_SIZE_LIMIT = 256


@dataclass(slots=True)
class RuntimeComponents:
    settings: Settings
    readiness: StartupReadiness
    extensions: tuple[BusinessExtension, ...]
    logging_runtime: LoggingRuntime

    @property
    def extension(self) -> BusinessExtension:
        """单策略快捷访问；多策略并存接入时由调用侧改为按 routing_key 选择。"""

        if not self.extensions:
            raise RuntimeError("no extensions configured")
        if len(self.extensions) > 1:
            raise RuntimeError("multiple extensions are configured; pick one explicitly")
        return self.extensions[0]
    gamma_client: GammaClient
    clob_client: ClobClient
    data_client: DataClient
    trading_client: PolymarketTradingClient | None
    polymarket_ws_client: PolymarketWebSocketClient
    event_bus: EventBus
    registry: MarketRegistry
    outbox: LocalOutbox
    db_engine: AsyncEngine
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
    sports_live_state_client: SportsLiveAggregateClient | None
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
    admin_service: AdminService | None = None
    sse_subscription_registry: SseSubscriptionRegistry | None = None
    bootstrap_summary: dict[str, Any] = field(default_factory=dict)
    season_state_store: SeasonStateStore | None = None
    season_state_worker: SportsSeasonStateWorker | None = None
    season_odds_worker: SportsSeasonOddsWorker | None = None
    season_state_client: EspnStandingsClient | None = None
    season_odds_client: SeasonOddsClient | None = None
    series_state_worker: SeriesStateWorker | None = None
    series_state_client: SeriesStateClient | None = None
    game_odds_worker: GameOddsWorker | None = None
    game_odds_client: GameOddsClient | None = None
    pregame_worker: GoalservePregameWorker | None = None
    pregame_client: GoalservePregameOddsClient | None = None
    parameter_store: ParameterStore | None = None


def _build_sports_live_state_client(
    settings: Settings,
    *,
    league_source_priority: Mapping[str, Sequence[str]] | None = None,
    trusted_sources: Sequence[str] | None = None,
) -> SportsLiveAggregateClient:
    """构建 Goalserve 直播状态聚合客户端。

    inplay feed：IP 白名单认证（无需 API key），1 秒刷新，覆盖
      basketball/soccer/hockey/baseball/tennis/esports/amfootball/volleyball。
    livescore getfeed：API key 认证，5 秒刷新，覆盖
      cricket/handball/rugby/boxing/mma/golf/horse_racing/f1/motogp。
    proxy 仅在开发环境配置（GOALSERVE_PROXY=http://127.0.0.1:7890），生产留空直连。
    """
    goalserve = GoalserveClient(
        sports=settings.goalserve_sport_codes,
        timeout_s=settings.sports_live_state_timeout_s,
        proxy=settings.goalserve_proxy,
    )
    providers: list[tuple[str, Any]] = [("goalserve", goalserve.list_events)]
    closers: list[Any] = [goalserve.aclose]

    api_key_secret = settings.goalserve_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret is not None else None
    if settings.goalserve_livescore_enabled and api_key:
        livescore = GoalserveLivescoreClient(
            api_key=api_key,
            sports=settings.goalserve_livescore_sport_codes,
            base_url=settings.goalserve_livescore_base_url,
            timeout_s=settings.goalserve_livescore_timeout_s,
            proxy=settings.goalserve_proxy,
        )
        providers.append(("goalserve_livescore", livescore.list_events))
        closers.append(livescore.aclose)

    provider_timeout_s = max(
        settings.sports_live_state_timeout_s * _PROVIDER_TIMEOUT_MULTIPLIER,
        _PROVIDER_TIMEOUT_FLOOR_S,
    )
    return SportsLiveAggregateClient(
        providers=providers,
        closers=closers,
        provider_timeout_s=provider_timeout_s,
        cooldown_base_s=settings.sports_live_state_health_cooldown_base_s,
        eviction_s=settings.sports_live_state_health_eviction_s,
        league_source_priority=league_source_priority,
        trusted_sources=trusted_sources,
    )


def _build_season_state_worker(
    settings: Settings,
    *,
    store: SeasonStateStore,
    lifecycle_bus: InProcessLifecycleBus,
) -> tuple[SportsSeasonStateWorker, EspnStandingsClient] | tuple[None, None]:
    """按 settings 装配 sports_season_state_worker；未启用时返回 (None, None)。

    第二项是底层 httpx-backed client，用于运行时 shutdown 时关闭，避免泄漏连接。
    """

    if not settings.sports_season_state_enabled:
        return None, None
    league_codes = tuple(
        code.strip().lower()
        for code in settings.sports_season_state_leagues.split(",")
        if code.strip()
    )
    sources = {
        code.strip().lower()
        for code in settings.sports_season_state_sources.split(",")
        if code.strip()
    }
    if "espn" not in sources or not league_codes:
        return None, None
    client = EspnStandingsClient(
        leagues=league_codes,
        timeout_s=settings.sports_season_state_timeout_s,
    )
    worker = SportsSeasonStateWorker(
        snapshot_provider=client.fetch_snapshot,
        store=store,
        lifecycle_bus=lifecycle_bus,
        enabled=True,
        source="espn",
        leagues=league_codes,
    )
    return worker, client


def _build_season_odds_worker(
    settings: Settings,
    *,
    registry: MarketRegistry,
    entry_metadata_store: EntryMetadataStore,
    extension: BusinessExtension,
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
    _classifier = extension if isinstance(extension, MarketClassificationHooks) else None

    def _is_outright(market: Market) -> bool:
        return _classifier.is_outright_market(market) if _classifier is not None else False

    def _sport_key(market: Market) -> str | None:
        return _classifier.sport_key_for_season_odds(market) if _classifier is not None else None

    def _market_key(market: Market) -> str:
        return market.event_slug or market.market_slug or ""

    worker = SportsSeasonOddsWorker(
        odds_client=client,
        registry=registry,
        entry_metadata_store=entry_metadata_store,
        sport_key_for=_sport_key,
        is_outright_market=_is_outright,
        market_key_for=_market_key,
        ttl_seconds=settings.sports_season_odds_ttl_seconds,
        enabled=True,
        event_bus=event_bus,
    )
    return worker, client


def _build_series_state_worker(
    settings: Settings,
    *,
    registry: MarketRegistry,
    entry_metadata_store: EntryMetadataStore,
    extension: BusinessExtension,
    event_bus: EventBus | None = None,
) -> tuple[SeriesStateWorker, SeriesStateClient] | tuple[None, None]:
    """按 settings 装配 series_state_worker。

    未启用 series state 子系统时返回 (None, None)；启用后用 ESPN scoreboard。
    """

    if not settings.sports_series_state_enabled:
        return None, None
    client = EspnSeriesStateClient(
        base_url=settings.sports_series_state_base_url,
        timeout_s=settings.sports_series_state_timeout_s,
    )

    _classifier = extension if isinstance(extension, MarketClassificationHooks) else None

    def _sport_key(market: Market) -> str | None:
        return _classifier.sport_key_for_series_state(market) if _classifier is not None else None

    def _is_series_winner(market: Market) -> bool:
        return _classifier.is_series_winner_market(market) if _classifier is not None else False

    def _series_key(market: Market) -> str | None:
        # event_slug 是稳定可读 key（"celtics-vs-knicks-2026-series" 类）；
        # 测试 / mock client 可按需匹配。底层 ESPN payload 用 event id / 短名
        # 模糊命中，所以这里返回最丰富的 event_slug + market_question 拼接。
        if market.event_slug:
            return market.event_slug
        if market.market_question:
            return market.market_question
        return market.market_slug or None

    worker = SeriesStateWorker(
        client=client,
        registry=registry,
        entry_metadata_store=entry_metadata_store,
        sport_key_for=_sport_key,
        is_series_winner_market=_is_series_winner,
        series_key_for=_series_key,
        ttl_seconds=settings.sports_series_state_ttl_seconds,
        enabled=True,
        event_bus=event_bus,
    )
    return worker, client


def _build_game_odds_worker(
    settings: Settings,
    *,
    registry: MarketRegistry,
    entry_metadata_store: EntryMetadataStore,
    extension: BusinessExtension,
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

    _classifier = extension if isinstance(extension, MarketClassificationHooks) else None

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
        entry_metadata_store=entry_metadata_store,
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
    worker = GoalservePregameWorker(client=client, enabled=True)
    return worker, client


def _validate_extension_config(extension: BusinessExtension, settings: Settings) -> tuple[ConfigIssue, ...]:
    """如扩展实现了 ConfigValidator 协议，则在启动期收集其拒绝原因。

    与 ``Settings.validate_startup_readiness`` 互补：把策略侧的最小可执行集
    校验（比如 discovery 列表是否为空）也前移到启动期，避免上线后才暴露。

    ConfigValidator 是可选 Protocol；未实现的 extension 直接返回空 issues。
    """

    if not isinstance(extension, ConfigValidator):
        return ()
    issues = extension.validate_config(settings)
    if not issues:
        return ()
    return tuple(issues)


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
    entry_metadata_store = EntryMetadataStore()
    outbox = LocalOutbox(max_size=settings.persistence_event_queue_max_size)
    event_bus.bind_persistence_sink(build_domain_event_outbox_sink(outbox))
    sse_subscription_registry = SseSubscriptionRegistry(soft_cap=settings.sse_subscriber_cap)
    event_bus.add_broadcast_listener(sse_subscription_registry.broadcast)
    db_engine = build_engine(settings.database_url)
    db_session_factory = build_session_factory(settings.database_url)
    persistence_repository = DatabasePersistenceRepository(db_session_factory)
    account_state_store = AccountStateStore()
    # 不预填假 balance：reconcile worker 会在启动后立刻调
    # ``clob_client.get_balance_allowance()`` 写入真实链上 USDC。预填会让
    # ``peak_bankroll_usdc`` 被 placeholder 值锚住，等真实 balance 写入后立刻
    # 触发 drawdown lockout（peak=placeholder >> 真实 balance）。在 reconcile
    # 拿到第一个权威值前，bankroll=0 → Kelly 全拒，正是安全态。
    lifecycle_bus = InProcessLifecycleBus()
    parameter_store = ParameterStore(event_bus=event_bus)
    extension_ports = build_extension_ports(
        registry=registry,
        snapshot_provider=account_state_store.snapshot,
        lifecycle_bus=lifecycle_bus,
        parameter_store=parameter_store,
        metrics_registry=metrics,
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
    extension_issues = _validate_extension_config(extension, settings)
    if extension_issues:
        raise ConfigLoadError(list(extension_issues))
    # 从扩展侧读取策略配置实例（kelly_* 等策略参数）；未实现 ConfiguredExtension 协议的
    # 扩展使用框架侧默认值兜底（不会出现在当前策略，仅防御性保留）。
    strategy_config = resolve_kelly_params(extension)
    # settings 是框架侧不变量，由 composition root 直接绑定。strategy.* 默认值由
    # 策略自己在 __init__ 时通过 ports.parameter.register_strategy_defaults 注册——
    # 框架不读策略私有属性，避免跨层 duck-typing。
    parameter_store.bind_settings(settings)
    # strategy_id 来自策略 spec，单进程内只装配一次；所有 framework worker / service
    # （persistence、decision recorder、trading_decision_service、user_ws_worker ...）
    # 都绑定同一个值，作为 SCOPE 表的归属键。
    strategy_id = extension.spec.strategy_id
    persistence_worker = PersistenceWorker(
        strategy_id=strategy_id,
        outbox=outbox,
        repository=persistence_repository,
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
    decision_recorder = DecisionEventRecorder(outbox=outbox, strategy_id=strategy_id)
    trading_decision_service = TradingDecisionService(
        extension_hooks=extension.hooks,
        strategy_id=strategy_id,
        registry=registry,
        orderbook_reader=market_ws_worker.snapshot,
        decision_recorder=decision_recorder,
    )
    trading_service = TradingService(
        executor=order_executor,
        lifecycle_bus=lifecycle_bus,
        event_bus=event_bus,
    )
    user_ws_worker = UserWsWorker(
        strategy_id=strategy_id,
        event_bus=event_bus,
        account_state_store=account_state_store,
    )

    _exposure_classifier = extension if isinstance(extension, MarketClassificationHooks) else None

    def _portfolio_exposure_metadata(
        snapshot: AccountSnapshot | None,
        current_condition_id: str | None,
        current_event_slug: str | None,
    ) -> dict[str, str]:
        """按 market family 聚合持仓 + open BUY 敞口，供策略风控读取。

        跳过当前 market 自身持仓（risk check 用 proposed_amount 对比），
        只聚合其他 outright / series market 的既有敞口。
        在 entry_metadata 热路径同步执行：仅内存遍历 + O(1) registry 查询，无 IO。
        """
        if snapshot is None or _exposure_classifier is None:
            return {}
        positions = snapshot.positions
        open_orders = snapshot.open_orders
        if not positions and not open_orders:
            return {}

        orders_by_condition: dict[str, list] = {}
        for order in open_orders:
            orders_by_condition.setdefault(order.condition_id, []).append(order)

        outright_total = Decimal("0")
        outright_event = Decimal("0")
        series_total = Decimal("0")
        series_event = Decimal("0")

        for position in positions:
            if position.condition_id == current_condition_id:
                continue
            pos_market = registry.get_by_condition_id(position.condition_id)
            if pos_market is None:
                continue
            family_label = _exposure_classifier.market_family_label(pos_market)
            pos_orders = orders_by_condition.get(position.condition_id, ())
            exposure = current_exposure_usdc(position, pos_orders)
            if family_label == "outright":
                outright_total += exposure
                if current_event_slug and pos_market.event_slug == current_event_slug:
                    outright_event += exposure
            elif family_label == "series":
                series_total += exposure
                if current_event_slug and pos_market.event_slug == current_event_slug:
                    series_event += exposure

        result: dict[str, str] = {}
        if outright_total:
            result["outright_total_exposure_usdc"] = str(outright_total)
            result["outright_event_exposure_usdc"] = str(outright_event)
        if series_total:
            result["series_total_exposure_usdc"] = str(series_total)
            result["series_event_exposure_usdc"] = str(series_event)
        return result

    def entry_metadata_for_event(event, snapshot: AccountSnapshot | None):
        market = None
        if event.condition_id is not None:
            market = registry.get_by_condition_id(event.condition_id)
        if market is None and event.token_id is not None:
            market = registry.get_by_token_id(event.token_id)
        if market is None and event.market_slug is not None:
            market = registry.get_by_slug(event.market_slug)
        base = entry_metadata_store.metadata_for_event(event, market=market)
        exposure = _portfolio_exposure_metadata(
            snapshot,
            current_condition_id=market.condition_id if market else event.condition_id,
            current_event_slug=market.event_slug if market else event.event_slug,
        )
        if exposure:
            return {**base, **exposure}
        return base

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
        kelly_fraction=strategy_config.kelly_fraction,
        kelly_max_position_fraction=strategy_config.kelly_max_position_fraction,
        kelly_min_edge=strategy_config.kelly_min_edge,
        kelly_min_stake_usdc=strategy_config.kelly_min_stake_usdc,
        kelly_allow_round_up_to_market_min=strategy_config.kelly_allow_round_up_to_market_min,
        kelly_round_up_max_overbet_ratio=strategy_config.kelly_round_up_max_overbet_ratio,
        kelly_drawdown_halt_fraction=strategy_config.kelly_drawdown_halt_fraction,
        order_retry_limit=settings.order_retry_limit,
        entry_metadata_provider=entry_metadata_for_event,
        parameter_store=parameter_store,
    )
    reconcile_service = ReconcileService(
        extension_hooks=extension.hooks,
        strategy_id=strategy_id,
        entry_metadata_provider=entry_metadata_for_market,
        orderbook_reader=trading_decision_service.lookup_orderbook,
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
        lifecycle_bus=lifecycle_bus,
    )
    market_discovery_worker = MarketDiscoveryWorker(
        market_service=market_service,
        event_bus=event_bus,
        retry_delay_seconds=MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS,
    )
    market_discovery_scan = FullMarketDiscoveryState()
    sports_live_state_client: SportsLiveAggregateClient | None = None
    sports_live_state_worker: SportsLiveStateWorker | None = None
    if settings.sports_live_state_enabled:
        live_state_hooks = extension.live_state_hooks
        if live_state_hooks is None:
            logger.warning(
                "sports live state sync skipped because extension does not implement LiveStateHooks",
                extra={"extension": extension.spec.name},
            )
        else:
            league_source_priority = live_state_hooks.league_source_affinity
            sports_live_state_client = _build_sports_live_state_client(
                settings,
                league_source_priority=league_source_priority,
                trusted_sources=None,  # 默认走 aggregate 内置 _DEFAULT_OFFICIAL_SOURCES
            )
            sports_live_state_worker = SportsLiveStateWorker(
                snapshot_provider=sports_live_state_client.list_events,
                match_live_state=live_state_hooks.match_live_state,
                registry=registry,
                entry_metadata_store=entry_metadata_store,
                event_bus=event_bus,
                market_tracker=market_ws_worker.track_market,
                lifecycle_bus=lifecycle_bus,
                enabled=True,
                source="sports_live_aggregate",
                leagues=settings.sports_live_state_league_codes,
                publish_entry_signals=settings.sports_live_state_publish_entry_signals,
            )
    season_state_store = SeasonStateStore()
    bind_extension_season_state(extension_ports, season_state_store)
    season_state_worker, season_state_client = _build_season_state_worker(
        settings,
        store=season_state_store,
        lifecycle_bus=lifecycle_bus,
    )
    season_odds_worker, season_odds_client = _build_season_odds_worker(
        settings,
        registry=registry,
        entry_metadata_store=entry_metadata_store,
        extension=extension,
        event_bus=event_bus,
    )
    series_state_worker, series_state_client = _build_series_state_worker(
        settings,
        registry=registry,
        entry_metadata_store=entry_metadata_store,
        extension=extension,
        event_bus=event_bus,
    )
    game_odds_worker, game_odds_client = _build_game_odds_worker(
        settings,
        registry=registry,
        entry_metadata_store=entry_metadata_store,
        extension=extension,
        event_bus=event_bus,
    )
    pregame_worker, pregame_client = _build_pregame_worker(settings)
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
    trading_decision_worker.bind_heartbeat(
        lambda **kwargs: supervisor.heartbeat_worker("trading_decision", **kwargs)
    )
    return RuntimeComponents(
        settings=settings,
        readiness=readiness,
        extensions=(extension,),
        logging_runtime=logging_runtime,
        parameter_store=parameter_store,
        gamma_client=gamma_client,
        clob_client=clob_client,
        data_client=data_client,
        trading_client=trading_client,
        polymarket_ws_client=polymarket_ws_client,
        event_bus=event_bus,
        registry=registry,
        outbox=outbox,
        db_engine=db_engine,
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
        season_state_store=season_state_store,
        season_state_worker=season_state_worker,
        season_odds_worker=season_odds_worker,
        season_state_client=season_state_client,
        season_odds_client=season_odds_client,
        series_state_worker=series_state_worker,
        series_state_client=series_state_client,
        game_odds_worker=game_odds_worker,
        game_odds_client=game_odds_client,
        pregame_worker=pregame_worker,
        pregame_client=pregame_client,
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
        sse_subscription_registry=sse_subscription_registry,
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

    with suppress(Exception):
        await runtime.order_executor.aclose()
    for client in (runtime.gamma_client, runtime.clob_client, runtime.data_client):
        with suppress(Exception):
            await client.aclose()
    if runtime.sports_live_state_client is not None:
        with suppress(Exception):
            await runtime.sports_live_state_client.aclose()
    if runtime.season_state_client is not None:
        with suppress(Exception):
            await runtime.season_state_client.aclose()
    if runtime.season_odds_client is not None:
        with suppress(Exception):
            await runtime.season_odds_client.aclose()
    if runtime.series_state_client is not None:
        with suppress(Exception):
            await runtime.series_state_client.aclose()
    if runtime.game_odds_client is not None:
        with suppress(Exception):
            await runtime.game_odds_client.aclose()
    if runtime.pregame_client is not None:
        with suppress(Exception):
            await runtime.pregame_client.close()
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
    runtime.supervisor.register_worker("admin_api", priority="P3", state=WorkerLifecycleState.RUNNING)
    runtime.supervisor.register_worker("market_discovery", priority="P2")
    runtime.supervisor.register_worker("market_ws", priority="P0", state=WorkerLifecycleState.PAUSED)
    runtime.supervisor.register_worker("user_ws", priority="P0", state=WorkerLifecycleState.PAUSED)
    runtime.supervisor.register_worker("trading_decision", priority="P0")
    runtime.supervisor.register_worker("reconcile", priority="P2")
    if runtime.sports_live_state_worker is not None:
        runtime.supervisor.register_worker("sports_live_state_sync", priority="P2")
    if runtime.season_state_worker is not None:
        runtime.supervisor.register_worker("sports_season_state_sync", priority="P2")
    if runtime.season_odds_worker is not None:
        runtime.supervisor.register_worker("sports_season_odds_sync", priority="P2")
    if runtime.series_state_worker is not None:
        runtime.supervisor.register_worker("sports_series_state_sync", priority="P2")
    if runtime.game_odds_worker is not None:
        runtime.supervisor.register_worker("sports_game_odds_sync", priority="P2")
    if runtime.pregame_worker is not None:
        runtime.supervisor.register_worker("sports_pregame_odds_sync", priority="P2")
    runtime.supervisor.register_worker("persistence", priority="P3")
    runtime.supervisor.register_worker("audit_retention_purge", priority="P3", state=WorkerLifecycleState.RUNNING, detail="scheduler-driven; first run after interval")


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


def _restore_trackable_markets(runtime: RuntimeComponents, markets: Iterable[Market]) -> int:
    """按当前业务扩展重新校验数据库恢复出的 market。

    数据库快照只作为恢复参考，不能把旧策略留下的 eligible 状态直接恢复成
    运行时真相；启动时必须重新走当前策略的 universe 与保留规则。
    """

    restored = 0
    account_snapshot = runtime.account_state_store.snapshot()
    hooks = runtime.extension.hooks
    for market in markets:
        universe_decision = hooks.select_market(market)
        if universe_decision.selected:
            runtime.market_ws_worker.track_market(market)
            restored += 1
            continue
        if not hooks.should_keep_tracking(market, account_snapshot):
            continue
        tracked_market = hooks.build_filtered_tracking_market(
            market,
            existing_market=market,
            reason=universe_decision.reason,
        )
        runtime.market_ws_worker.track_market(tracked_market)
        restored += 1
    return restored


async def _handle_market_ws_message(runtime, message) -> None:
    await handle_market_ws_message(runtime, message)


async def _load_reference_state(runtime: RuntimeComponents) -> dict[str, int]:
    loaded = {"markets": 0, "positions": 0, "open_orders": 0, "fills": 0, "account_snapshots": 0}
    try:
        async with runtime.db_session_factory() as session:
            account_snapshot = await AccountSnapshotRepository(session).get_current_snapshot()
            markets = await MarketRepository(session).list_markets_snapshot(limit=_STARTUP_SNAPSHOT_ITEM_LIMIT, offset=0)
            positions = await PositionRepository(session).list_positions_snapshot(limit=_STARTUP_SNAPSHOT_ITEM_LIMIT, offset=0)
            open_orders = await OrderRepository(session).list_open_orders_snapshot(limit=_STARTUP_SNAPSHOT_ITEM_LIMIT, offset=0)
            fills = await FillRepository(session).list_fills_snapshot(limit=_STARTUP_SNAPSHOT_ITEM_LIMIT, offset=0)
        if account_snapshot is not None:
            # peak 必须先恢复——否则 update_balances 触发的 publish 会用 in-memory 0
            # 当 baseline，把"重启前历史 peak 1500，当前 600"误算成 peak=600，drawdown
            # lockout 永远不触发。先 restore 再 update_balances，publish 时取 max。
            runtime.account_state_store.restore_peak_bankroll(account_snapshot.peak_bankroll_usdc)
            _restore_account_reference_state(
                runtime,
                balance_usdc=account_snapshot.balance_usdc,
                allowance_usdc=account_snapshot.allowance_usdc,
            )
        runtime.account_state_store.replace_positions(positions.items)
        runtime.account_state_store.replace_open_orders(open_orders.items)
        runtime.account_state_store.replace_fills(fills.items)
        restored_markets = _restore_trackable_markets(runtime, markets.items)
        loaded = {
            "account_snapshots": 0 if account_snapshot is None else 1,
            "markets": restored_markets,
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
    if runtime.season_state_worker is not None:
        runtime.scheduler.register_job(
            "sports_season_state_sync",
            lambda: _run_sports_season_state_sync(runtime),
            priority="P2",
            interval_seconds=float(runtime.settings.sports_season_state_interval_seconds),
            tags=("sports_season_state",),
            start=True,
            run_immediately=True,
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
    if runtime.series_state_worker is not None:
        runtime.scheduler.register_job(
            "sports_series_state_sync",
            lambda: _run_sports_series_state_sync(runtime),
            priority="P2",
            interval_seconds=float(runtime.settings.sports_series_state_interval_seconds),
            tags=("sports_series_state",),
            start=True,
            # 启动时不立即跑：市场发现 + 注册表填充通常需要 10-30 秒；
            # 立即跑会在 registry 为空时找不到 targets，浪费一整个 interval。
            run_immediately=False,
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
        run_immediately=False,
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
                f"events={result.events_seen} matches={result.matches} "
                f"signals={result.entry_signals_published}"
            ),
        )
    _sync_runtime_metrics(runtime)


async def _run_sports_season_state_sync(runtime: RuntimeComponents) -> None:
    worker = runtime.season_state_worker
    if worker is None:
        return
    runtime.supervisor.heartbeat_worker("sports_season_state_sync", detail="syncing")
    try:
        snapshot = await worker.sync_once()
    except Exception as exc:
        runtime.supervisor.mark_worker_error(
            "sports_season_state_sync",
            detail="sync_failed",
            last_error=str(exc),
        )
        raise
    if snapshot is None:
        runtime.supervisor.heartbeat_worker(
            "sports_season_state_sync",
            state=WorkerLifecycleState.PAUSED,
            detail="disabled",
        )
    else:
        runtime.supervisor.heartbeat_worker(
            "sports_season_state_sync",
            detail=f"standings={len(snapshot.standings)} series={len(snapshot.series)}",
        )


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


async def _run_sports_series_state_sync(runtime: RuntimeComponents) -> None:
    worker = runtime.series_state_worker
    if worker is None:
        return
    runtime.supervisor.heartbeat_worker("sports_series_state_sync", detail="syncing")
    try:
        refreshed = await worker.sync_once()
    except Exception as exc:
        runtime.supervisor.mark_worker_error(
            "sports_series_state_sync",
            detail="sync_failed",
            last_error=str(exc),
        )
        raise
    runtime.supervisor.heartbeat_worker(
        "sports_series_state_sync",
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


async def _run_audit_retention_purge(runtime: RuntimeComponents) -> None:
    """每天清理一次 audit_events 表中超出保留期的行。

    P3 后台任务——所有 DB 异常都在 purge_audit_events_once 内部 catch + 日志。
    """

    from polymarket_trader.app.audit_retention import purge_audit_events_once

    summary = await purge_audit_events_once(
        runtime.db_session_factory,
        retention_days=runtime.settings.audit_retention_days,
        batch_size=runtime.settings.audit_retention_purge_batch_size,
    )
    runtime.supervisor.heartbeat_worker(
        "audit_retention_purge",
        detail=(
            f"deleted={summary['deleted_rows']} batches={summary['batches']} "
            f"cutoff={summary.get('cutoff') or '-'} "
            f"skipped={summary['skipped']} err={'!' if summary['error'] else '-'}"
        ),
    )


async def _run_settlement_scan(runtime: RuntimeComponents) -> None:
    """运行一次结算扫描。任何异常仅记日志——不阻塞 supervisor。"""

    from polymarket_trader.app.settlement_scanner import SettlementScannerService

    async def _list_positions() -> list[Position]:
        try:
            async with runtime.db_session_factory() as session:
                page = await PositionRepository(session).list_positions_snapshot(
                    limit=200,
                    offset=0,
                )
                return list(page.items or ())
        except Exception:
            logger.warning("settlement_scanner.positions_query_failed", exc_info=True)
            return []

    async def _audit_query(**kwargs: Any) -> RepositoryPage[AuditEvent]:
        async with runtime.db_session_factory() as session:
            return await AuditEventRepository(session).list_audit_events_snapshot(**kwargs)

    async def _gamma_by_condition(condition_id: str) -> Any | None:
        """Gamma ``/markets/{id}`` 用内部数值 id，不接受 condition_id；用
        ``condition_ids`` 过滤拉一行。``list_markets_by_params`` 一次最多返回
        一个匹配（同一条 condition_id 对应一个 market）。"""

        try:
            markets = await runtime.gamma_client.list_markets_by_params(
                {"condition_ids": condition_id, "limit": 1}
            )
        except Exception:
            logger.info(
                "settlement_scanner.gamma_filter_failed",
                extra={"condition_id": condition_id},
                exc_info=True,
            )
            return None
        return markets[0] if markets else None

    positions = await _list_positions()
    service = SettlementScannerService(
        gamma_market_by_condition=_gamma_by_condition,
        positions_provider=lambda: positions,
        audit_events_query=_audit_query,
        event_bus=runtime.event_bus,
    )
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
