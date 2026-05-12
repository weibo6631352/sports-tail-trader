from __future__ import annotations

import asyncio
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polymarket_trader.api.routes.stream import SseSubscriptionRegistry
from polymarket_trader.app.market_service import MarketService
from polymarket_trader.app.ports import bind_extension_orderbook_reader, build_extension_ports
from polymarket_trader.app.reconcile_service import ReconcileService
from polymarket_trader.app.extension_host import load_extension
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.config import ConfigIssue, ConfigLoadError, Settings, StartupReadiness, load_settings
from polymarket_trader.domain.events import DomainEvent, OutboxPriority
from polymarket_trader.domain.market import Market
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
from polymarket_trader.infra.sports import (
    EspnScoreboardClient,
    EspnStandingsClient,
    MlbStatsApiClient,
    NbaLiveScoreboardClient,
    NhlScoreApiClient,
    PandascoreLiveClient,
    SofaScoreLiveClient,
    SportsLiveAggregateClient,
    TheOddsApiClient,
    TheSportsDbLiveClient,
    sofascore_sports_for_leagues,
    thesportsdb_sports_for_leagues,
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
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.lifecycle_bus import InProcessLifecycleBus
from polymarket_trader.runtime.metrics_sync import sync_runtime_metrics as _sync_runtime_metrics
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.runtime.ws_loops import (
    handle_market_ws_message,
    run_market_ws as _run_market_ws,
    run_user_ws as _run_user_ws,
)
from polymarket_trader.extension_api import BusinessExtension
from polymarket_trader.workers.market_discovery_worker import MarketDiscoveryWorker
from polymarket_trader.workers.market_ws import MarketWsWorker
from polymarket_trader.workers.persistence import PersistenceWorker
from polymarket_trader.workers.reconcile import ReconcileWorker
from polymarket_trader.workers.sports_live_state_worker import SportsLiveStateWorker
from polymarket_trader.workers.sports_season_odds_worker import SportsSeasonOddsWorker
from polymarket_trader.workers.sports_season_state_worker import SportsSeasonStateWorker
from polymarket_trader.workers.trading_decision import TradingDecisionWorker
from polymarket_trader.workers.user_ws import UserWsWorker

logger = logging.getLogger(__name__)


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
    admin_service: object | None = None
    sse_subscription_registry: SseSubscriptionRegistry | None = None
    bootstrap_summary: dict[str, Any] = field(default_factory=dict)
    season_state_store: SeasonStateStore | None = None
    season_state_worker: SportsSeasonStateWorker | None = None
    season_odds_worker: SportsSeasonOddsWorker | None = None
    season_state_client: Any | None = None
    season_odds_client: Any | None = None
    parameter_store: Any | None = None


def _build_sports_live_state_client(settings: Settings) -> SportsLiveAggregateClient:
    """按配置构建多源体育直播状态聚合器。"""

    source_codes = set(settings.sports_live_state_source_codes)
    league_codes = set(settings.sports_live_state_league_codes)
    providers = []
    closers = []
    if "espn" in source_codes:
        espn_leagues = tuple(
            league
            for league in settings.sports_live_state_league_codes
            if league in settings._ESPN_SUPPORTED_LEAGUES or league.startswith("soccer:")
        )
        espn_client = EspnScoreboardClient(
            base_url=settings.sports_live_state_espn_base_url,
            leagues=espn_leagues,
            timeout_s=settings.sports_live_state_timeout_s,
        )
        providers.append(("espn", espn_client.list_games))
        closers.append(espn_client.aclose)
    if "nba" in source_codes and "nba" in league_codes:
        nba_client = NbaLiveScoreboardClient(
            base_url=settings.sports_live_state_nba_base_url,
            timeout_s=settings.sports_live_state_timeout_s,
        )
        providers.append(("nba", nba_client.list_games))
        closers.append(nba_client.aclose)
    if "nhl" in source_codes and "nhl" in league_codes:
        nhl_client = NhlScoreApiClient(
            base_url=settings.sports_live_state_nhl_base_url,
            timeout_s=settings.sports_live_state_timeout_s,
        )
        providers.append(("nhl", nhl_client.list_games))
        closers.append(nhl_client.aclose)
    if "mlb" in source_codes and "mlb" in league_codes:
        mlb_client = MlbStatsApiClient(
            base_url=settings.sports_live_state_mlb_base_url,
            timeout_s=settings.sports_live_state_timeout_s,
        )
        providers.append(("mlb", mlb_client.list_games))
        closers.append(mlb_client.aclose)
    if "sofascore" in source_codes:
        sofascore_sports = sofascore_sports_for_leagues(settings.sports_live_state_league_codes)
        if sofascore_sports:
            sofascore_client = SofaScoreLiveClient(
                base_url=settings.sports_live_state_sofascore_base_url,
                sports=sofascore_sports,
                league_codes=settings.sports_live_state_league_codes,
                timeout_s=settings.sports_live_state_timeout_s,
                lookback_days=settings.sports_live_state_sofascore_lookback_days,
                lookahead_days=settings.sports_live_state_sofascore_lookahead_days,
            )
            providers.append(("sofascore", sofascore_client.list_games))
            closers.append(sofascore_client.aclose)
    if "thesportsdb" in source_codes:
        thesportsdb_sports = thesportsdb_sports_for_leagues(settings.sports_live_state_league_codes)
        if thesportsdb_sports:
            thesportsdb_client = TheSportsDbLiveClient(
                base_url=settings.sports_live_state_thesportsdb_base_url,
                sports=thesportsdb_sports,
                league_codes=settings.sports_live_state_league_codes,
                timeout_s=settings.sports_live_state_timeout_s,
            )
            providers.append(("thesportsdb", thesportsdb_client.list_games))
            closers.append(thesportsdb_client.aclose)
    if "pandascore" in source_codes:
        token_secret = settings.sports_live_state_pandascore_token
        token_value = token_secret.get_secret_value() if token_secret is not None else None
        if token_value:
            videogames = tuple(
                slug.strip().lower()
                for slug in settings.sports_live_state_pandascore_videogames.split(",")
                if slug.strip()
            )
            pandascore_client = PandascoreLiveClient(
                base_url=settings.sports_live_state_pandascore_base_url,
                api_token=token_value,
                videogame_slugs=videogames or None,
                timeout_s=settings.sports_live_state_timeout_s,
            )
            providers.append(("pandascore", pandascore_client.list_games))
            closers.append(pandascore_client.aclose)
        # 缺 token 时不注册 provider；启动期校验里会有 warning，避免 source_statuses 每 5 秒重复报错。
    # 聚合器超时是“单个 provider 完整快照”的预算；像 ESPN/SofaScore 这类 provider
    # 内部会按多个 sport/league 拉取，预算需要高于单次 HTTP timeout，避免刚拿到部分
    # 实盘数据时被外层取消。各 provider 已并行隔离，放宽这里不会阻塞交易主链路。
    provider_timeout_s = max(settings.sports_live_state_timeout_s * 3, 12.0)
    return SportsLiveAggregateClient(
        providers=tuple(providers),
        closers=tuple(closers),
        provider_timeout_s=provider_timeout_s,
        cooldown_base_s=settings.sports_live_state_health_cooldown_base_s,
        eviction_s=settings.sports_live_state_health_eviction_s,
    )


def _build_season_state_worker(
    settings: Settings,
    *,
    store: SeasonStateStore,
    lifecycle_bus: Any,
) -> tuple[SportsSeasonStateWorker, Any] | tuple[None, None]:
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
    extension: Any,
) -> tuple[SportsSeasonOddsWorker, Any] | tuple[None, None]:
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
    hooks = getattr(extension, "hooks", extension)
    select_market = getattr(hooks, "select_market", None)

    def _is_outright(market: Any) -> bool:
        if select_market is None:
            return False
        try:
            decision = select_market(market)
        except Exception:
            return False
        return bool(decision.selected) and decision.metadata.get("market_family") == "outright"

    def _sport_key(market: Any) -> str | None:
        # 简单映射：从 tags / category 推断。NBA/NHL/NFL/MLB 等明确 league 直接转 TheOddsAPI sport_key。
        text = " ".join(filter(None, (
            getattr(market, "category", None) or "",
            *(getattr(market, "tags", ()) or ()),
        ))).lower()
        if "nba" in text or "basketball" in text:
            return "basketball_nba"
        if "nhl" in text or "hockey" in text:
            return "icehockey_nhl"
        if "nfl" in text or "american football" in text:
            return "americanfootball_nfl"
        if "mlb" in text or "baseball" in text:
            return "baseball_mlb"
        if "epl" in text or "premier league" in text:
            return "soccer_epl"
        return None

    def _market_key(market: Any) -> str:
        return getattr(market, "event_slug", None) or getattr(market, "market_slug", None) or ""

    worker = SportsSeasonOddsWorker(
        odds_client=client,
        registry=registry,
        entry_metadata_store=entry_metadata_store,
        sport_key_for=_sport_key,
        is_outright_market=_is_outright,
        market_key_for=_market_key,
        ttl_seconds=settings.sports_season_odds_ttl_seconds,
        enabled=True,
    )
    return worker, client


def _validate_extension_config(extension: Any, settings: Settings) -> tuple[ConfigIssue, ...]:
    """如扩展实现了 ConfigValidator 协议，则在启动期收集其拒绝原因。

    与 ``Settings.validate_startup_readiness`` 互补：把策略侧的最小可执行集
    校验（比如 discovery 列表是否为空）也前移到启动期，避免上线后才暴露。
    """

    validate = getattr(extension, "validate_config", None)
    if not callable(validate):
        return ()
    issues = validate(settings)
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
    sse_subscription_registry = SseSubscriptionRegistry()
    event_bus.add_broadcast_listener(sse_subscription_registry.broadcast)
    db_session_factory = build_session_factory(settings.database_url)
    persistence_repository = DatabasePersistenceRepository(db_session_factory)
    account_state_store = AccountStateStore()
    # 不预填假 balance：reconcile worker 会在启动后立刻调
    # ``clob_client.get_balance_allowance()`` 写入真实链上 USDC。预填会让
    # ``peak_bankroll_usdc`` 被 placeholder 值锚住，等真实 balance 写入后立刻
    # 触发 drawdown lockout（peak=placeholder >> 真实 balance）。在 reconcile
    # 拿到第一个权威值前，bankroll=0 → Kelly 全拒，正是安全态。
    lifecycle_bus = InProcessLifecycleBus()
    from polymarket_trader.app.parameter_store import ParameterStore

    parameter_store = ParameterStore(event_bus=event_bus)
    extension_ports = build_extension_ports(
        registry=registry,
        snapshot_provider=account_state_store.snapshot,
        lifecycle_bus=lifecycle_bus,
        parameter_store=parameter_store,
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
        kelly_fraction=settings.kelly_fraction,
        kelly_max_position_fraction=settings.kelly_max_position_fraction,
        kelly_min_edge=settings.kelly_min_edge,
        kelly_min_stake_usdc=settings.kelly_min_stake_usdc,
        kelly_allow_round_up_to_market_min=settings.kelly_allow_round_up_to_market_min,
        kelly_round_up_max_overbet_ratio=settings.kelly_round_up_max_overbet_ratio,
        kelly_drawdown_halt_fraction=settings.kelly_drawdown_halt_fraction,
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
                extra={"extension": getattr(extension.spec, "name", "unknown")},
            )
        else:
            sports_live_state_client = _build_sports_live_state_client(settings)
            sports_live_state_worker = SportsLiveStateWorker(
                snapshot_provider=sports_live_state_client.list_games,
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
    if runtime.season_state_worker is not None:
        runtime.supervisor.register_worker("sports_season_state_sync", priority="P2")
    if runtime.season_odds_worker is not None:
        runtime.supervisor.register_worker("sports_season_odds_sync", priority="P2")
    runtime.supervisor.register_worker("persistence", priority="P3")
    runtime.supervisor.register_worker("audit_retention_purge", priority="P3")


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
                f"games={result.games_seen} matches={result.matches} "
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
    from polymarket_trader.infra.db import AuditEventRepository, PositionRepository

    async def _list_positions() -> list[Any]:
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

    async def _audit_query(**kwargs: Any) -> Any:
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
