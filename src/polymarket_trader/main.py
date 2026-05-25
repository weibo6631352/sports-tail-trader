from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.app.admin_service import AdminService

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from polymarket_trader.api.routes.stream import SseSubscriptionRegistry
from polymarket_trader.app.market_service import MarketService
from polymarket_trader.app.ports import bind_extension_season_state, build_extension_ports
from polymarket_trader.app.reconcile_service import ReconcileService
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.config import ConfigIssue, ConfigLoadError, Settings, StartupReadiness, load_settings
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.allocation import current_exposure_usdc
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.position import Position
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
from polymarket_trader.app.paper import PaperSubmitOnlyOrderClient, PaperVirtualLedger
from polymarket_trader.infra.sports.goalserve_lazy_client import GoalserveLazyClient
from polymarket_trader.infra.sports import (
    GoalserveInplayClient,
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
from polymarket_trader.workers.market_discovery_worker import MarketDiscoveryWorker
from polymarket_trader.workers.market_ws import MarketWsWorker
from polymarket_trader.workers.persistence import PersistenceWorker
from polymarket_trader.workers.reconcile import ReconcileWorker, ReconcileWorkerResult
from polymarket_trader.workers.sports_live_state_worker import SportsLiveStateWorker
from polymarket_trader.workers.sports_season_odds_worker import SportsSeasonOddsWorker
from polymarket_trader.workers.game_odds_worker import GameOddsWorker
from polymarket_trader.workers.goalserve_pregame_worker import GoalservePregameWorker
from polymarket_trader.workers.trading_decision import TradingDecisionWorker
from polymarket_trader.quant.strategy import CurrentStrategy as _CurrentStrategy
from polymarket_trader.workers.user_ws import UserWsWorker
from polymarket_trader.infra.sports.game_odds_client import (
    GameOddsClient,
    TheOddsApiGameOddsClient,
)
from polymarket_trader.infra.sports.goalserve_livescore_client import (
    SPORT_CODE_TO_FEED_KEYS,
)
# composition root 直接读策略侧运动分类：livescore demand-driven 轮询需要把
# tracked market 映射到运动码，再映射到 feed key。polymarket_trader.quant 是当前装配
# 的业务扩展实现，main.py 作为 composition root 在此处接线属预期范围。
from polymarket_trader.quant.live_state import _market_sport_codes

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
    extensions: tuple[_CurrentStrategy, ...]
    logging_runtime: LoggingRuntime

    @property
    def extension(self) -> _CurrentStrategy:
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
    gamma_snapshot_store: GammaMarketSnapshotStore
    outbox: LocalOutbox
    db_engine: AsyncEngine
    db_session_factory: async_sessionmaker[AsyncSession]
    persistence_repository: DatabasePersistenceRepository
    persistence_worker: PersistenceWorker
    account_state_store: AccountStateStore
    entry_metadata_store: EntryMetadataStore
    order_executor: PolymarketOrderExecutor
    market_ws_worker: MarketWsWorker
    orderbook_delta_store: OrderbookDeltaStore
    orderbook_history_buffer: OrderbookHistoryBuffer
    orderbook_derived_store: OrderbookDerivedStore
    orderbook_derived_publisher: OrderbookDerivedPublisher
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


def _build_livescore_active_sports_provider(
    registry: MarketRegistry,
) -> Callable[[], frozenset[str]]:
    """构建 livescore demand-driven 轮询的 active_sports_provider。

    每轮轮询调用一次，遍历 tracked-market registry，返回"有需求"的 _SPORT_FEEDS
    key 集合：某市场 live（已开赛且未结束）或将在 60 分钟内开赛时，把它的运动码
    映射出的所有 feed key 纳入。无相关市场的运动整轮跳过 HTTP 抓取。

    必须廉价（每轮都调）：只做一次 registry 快照遍历 + 内存判断，不做任何 I/O。
    """

    # 同 inplay provider:Polymarket end_date 对体育单场市场常 == start_time,
    # 不能用 end > now 判 live。MLB/NBA/NHL/NFL/Tennis 单场 ≤ 6h + 1h buffer。
    _LIVESCORE_GAME_WINDOW = timedelta(hours=7)

    def _provider() -> frozenset[str]:
        now = datetime.now(timezone.utc)
        near_start_cutoff = now + timedelta(minutes=60)
        active: set[str] = set()
        for market in registry.snapshot().markets:
            start = market.game_start_time
            if start is not None and start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            end = market.end_date
            if end is not None and end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            # live：已开赛且在比赛持续期内(≤ 7h)。about-to-start：[now, now+60min]。
            is_live = (
                start is not None
                and start <= now
                and now <= start + _LIVESCORE_GAME_WINDOW
            )
            is_near_start = (
                start is not None and now <= start <= near_start_cutoff
            )
            # game_start_time 缺失兜底:用 end_date 在未来 6h 内判定为进行中/临近。
            start_unknown_active = start is None and (
                end is None or now < end < now + timedelta(hours=6)
            )
            if not (is_live or is_near_start or start_unknown_active):
                continue
            for code in _market_sport_codes(market):
                feed_keys = SPORT_CODE_TO_FEED_KEYS.get(code)
                if feed_keys:
                    active.update(feed_keys)
        return frozenset(active)

    return _provider


def _build_inplay_active_sports_provider(
    registry: MarketRegistry,
) -> Callable[[], frozenset[str]]:
    """构建 inplay GZIP feed demand-driven 轮询的 active_sports_provider。

    与 _build_livescore_active_sports_provider 同样遍历 tracked-market registry，
    但返回的是 _market_sport_codes 输出的**规范运动码**集合（football / basketball /
    ice-hockey 等）——GoalserveInplayClient 自己用 SPORT_CODE_TO_INPLAY_KEYS 把
    规范码映射到 feed 路径 token，因此这里不做 feed-key 映射。

    必须廉价（每轮都调）：只做一次 registry 快照遍历 + 内存判断，不做任何 I/O。
    """

    # MLB/NBA/NHL/NFL/Tennis 单场比赛持续时间上限 ≤ 6 小时,加 1h buffer 保证
    # 末段 inplay 仍拉。Polymarket end_date 对体育单场市场常 = game_start_time
    # (Gamma API 字段语义混淆),不能用 end > now 判 live。
    _GAME_INPLAY_WINDOW = timedelta(hours=7)

    def _provider() -> frozenset[str]:
        now = datetime.now(timezone.utc)
        near_start_cutoff = now + timedelta(minutes=60)
        active: set[str] = set()
        for market in registry.snapshot().markets:
            start = market.game_start_time
            if start is not None and start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            end = market.end_date
            if end is not None and end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            # is_live: 比赛已开始且未超过 _GAME_INPLAY_WINDOW(默认 7h)。
            # 之前用 end > now 是 bug — Polymarket end_date 对体育市场常 == start_time,
            # 会让已开赛市场永远判 not-live → inplay 不拉 → 122 个 missing live state。
            is_live = (
                start is not None
                and start <= now
                and now <= start + _GAME_INPLAY_WINDOW
            )
            is_near_start = (
                start is not None and now <= start <= near_start_cutoff
            )
            # game_start_time 缺失兜底:用 end_date 在未来 6h 内判定为进行中/临近。
            start_unknown_active = start is None and (
                end is None or now < end < now + timedelta(hours=6)
            )
            if not (is_live or is_near_start or start_unknown_active):
                continue
            active.update(_market_sport_codes(market))
        return frozenset(active)

    return _provider


def _build_sports_live_state_client(
    settings: Settings,
    *,
    league_source_priority: Mapping[str, Sequence[str]] | None = None,
    trusted_sources: Sequence[str] | None = None,
    livescore_active_sports_provider: Callable[[], frozenset[str]] | None = None,
    inplay_active_sports_provider: Callable[[], frozenset[str]] | None = None,
) -> SportsLiveAggregateClient:
    """构建 Goalserve 直播状态聚合客户端。

    inplay GZIP feed：keyless（IP 白名单），每 sport ~1s 刷新，demand-driven 轮询，
      覆盖 soccer/basket/tennis/volleyball/amfootball/esports/hockey/baseball。
    livescore getfeed：API key 认证，5 秒刷新，覆盖
      cricket/handball/rugby/boxing/mma/golf/horse_racing/f1/motogp。
    proxy 仅在开发环境配置（GOALSERVE_PROXY=http://127.0.0.1:7890），生产留空直连。

    *_active_sports_provider：传入时启用 demand-driven 轮询，只抓取有
      live/即将开赛 Polymarket 市场的运动 feed；None 则全量轮询。
    """
    api_key_secret = settings.goalserve_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret is not None else None

    inplay = GoalserveInplayClient(
        proxy=settings.goalserve_proxy,
        active_sports_provider=inplay_active_sports_provider,
    )
    providers: list[tuple[str, Any]] = [("goalserve_inplay", inplay.list_events)]
    closers: list[Any] = [inplay.aclose]
    status_providers: list[tuple[str, Any]] = [
        ("goalserve_inplay", inplay.inplay_per_sport_status)
    ]

    if settings.goalserve_livescore_enabled and api_key:
        livescore = GoalserveLivescoreClient(
            api_key=api_key,
            base_url=settings.goalserve_livescore_base_url,
            timeout_s=settings.goalserve_livescore_timeout_s,
            poll_interval_s=float(settings.sports_live_state_interval_seconds),
            proxy=settings.goalserve_proxy,
            active_sports_provider=livescore_active_sports_provider,
        )
        providers.append(("goalserve_livescore", livescore.list_events))
        closers.append(livescore.aclose)
        status_providers.append(("goalserve_livescore", livescore.livescore_per_sport_status))

    provider_timeout_s = max(
        settings.sports_live_state_timeout_s * _PROVIDER_TIMEOUT_MULTIPLIER,
        _PROVIDER_TIMEOUT_FLOOR_S,
    )
    return SportsLiveAggregateClient(
        providers=providers,
        closers=closers,
        status_providers=status_providers,
        provider_timeout_s=provider_timeout_s,
        cooldown_base_s=settings.sports_live_state_health_cooldown_base_s,
        eviction_s=settings.sports_live_state_health_eviction_s,
        league_source_priority=league_source_priority,
        trusted_sources=trusted_sources,
    )


def _build_season_odds_worker(
    settings: Settings,
    *,
    registry: MarketRegistry,
    entry_metadata_store: EntryMetadataStore,
    extension: _CurrentStrategy,
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
    _classifier = extension

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


def _build_game_odds_worker(
    settings: Settings,
    *,
    registry: MarketRegistry,
    entry_metadata_store: EntryMetadataStore,
    extension: _CurrentStrategy,
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

    _classifier = extension

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
    *,
    registry: MarketRegistry,
    entry_metadata_store: EntryMetadataStore,
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
        entry_metadata_store=entry_metadata_store,
    )
    return worker, client


def _validate_extension_config(extension: _CurrentStrategy, settings: Settings) -> tuple[ConfigIssue, ...]:
    """启动期收集策略侧配置拒绝原因，与 Settings.validate_startup_readiness 互补。"""

    issues = extension.validate_config(settings)
    return tuple(issues) if issues else ()


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
    gamma_snapshot_store = GammaMarketSnapshotStore()
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
    # ``clob_client.get_balance_allowance()`` 写入真实链上 USDC。在 reconcile
    # 拿到第一个权威值前，bankroll=0 → Kelly 全拒，正是安全态。
    lifecycle_bus = InProcessLifecycleBus()
    parameter_store = ParameterStore(event_bus=event_bus)
    runtime_ports = build_extension_ports(
        lifecycle_bus=lifecycle_bus,
        parameter_store=parameter_store,
        metrics_registry=metrics,
    )
    # 直接装配 quant 策略——量化决策器就是这个交易系统本身。
    from polymarket_trader.quant.strategy import CurrentStrategy
    from polymarket_trader.quant.config import load_current_strategy_config
    from polymarket_trader.quant.identity import STRATEGY_ID
    strategy_config = load_current_strategy_config(settings.extension_config_path)
    extension = CurrentStrategy(config=strategy_config, ports=runtime_ports)
    extension_issues = _validate_extension_config(extension, settings)
    if extension_issues:
        raise ConfigLoadError(list(extension_issues))
    parameter_store.bind_settings(settings)
    strategy_id = STRATEGY_ID
    persistence_worker = PersistenceWorker(
        strategy_id=strategy_id,
        outbox=outbox,
        repository=persistence_repository,
    )
    async def load_market_rest_snapshot(token_id: str):
        orderbook = await clob_client.get_orderbook(token_id)
        return orderbook.to_snapshot()

    orderbook_delta_store = OrderbookDeltaStore()
    # 纯时间窗 15s(覆盖 2/3/5/10s + 余量),无 maxlen 兜底.
    # max_tokens=2000 LRU evict 防极端 token 爆.
    orderbook_history_buffer = OrderbookHistoryBuffer(max_age_s=15.0, max_tokens=2000)
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
    registry.register_prune_callback(orderbook_derived_store.evict_market)
    # market prune 时同步清 gamma snapshot store——市场被回收后没人会查它的
    # gamma 元数据，留在 store 里只是内存浪费。
    registry.register_prune_callback(
        lambda cid, _tokens: gamma_snapshot_store.prune(cid)
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
    if settings.paper_trading_mode:
        paper_ledger = PaperVirtualLedger()
        paper_ledger.fund(settings.portfolio_budget_usdc)
        # paper 模式禁用真签名：py-clob-client.sign_order 会调链上 balance check,
        # 链上实际余额很少（多被 active orders 锁住）→ 立即报 "not enough balance"
        # 阻塞所有下单。paper 模式不上链,签名走本地虚拟即可,不损失策略验证价值。
        execution_client = PaperSubmitOnlyOrderClient(
            real_sign_client=None,
            market_lookup=registry.get_by_token_id,
            orderbook_lookup=market_ws_worker.snapshot,
            ledger=paper_ledger,
        )
        # account_state_store 必须用 paper_ledger 的虚拟余额，否则 RiskManager 看
        # 链上真实余额（$0.x 锁在 active orders 后）→ "available_usdc_below..." 警告 +
        # Kelly 算 stake=0 全部拒绝。初始一次性设值,启动一个后台 syncer 持续覆盖
        # reconcile worker 周期写回的真链上余额。
        account_state_store.update_balances(
            balance_usdc=settings.portfolio_budget_usdc,
            allowance_usdc=settings.portfolio_budget_usdc * Decimal("10"),
        )

        from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor as _SPM
        async def _paper_balance_syncer() -> None:
            """每秒把 paper_ledger 同步到 account_state_store（balance + positions）。

            balance: paper_ledger.available_usdc → account_state_store.balance_usdc
              （否则 reconcile 写回真链上 $0.x 余额阻塞 Kelly）
            positions: paper_ledger.positions → account_state_store.positions
              （否则策略读 stale account_state 持仓反复 reprice 已平仓 token，
              触发 simulate_fill 卖空 ledger 持仓 → available 凭空涨的 bug）
            """
            from polymarket_trader.domain.position import Position
            while True:
                try:
                    account_state_store.update_balances(
                        balance_usdc=paper_ledger.available_usdc,
                        allowance_usdc=settings.portfolio_budget_usdc * Decimal("10"),
                    )
                    # 把 ledger 持仓投射成 Position 同步回 account_state
                    paper_positions: list[Position] = []
                    for token_id, shares in paper_ledger.positions.items():
                        if shares <= Decimal("0"):
                            continue
                        market = registry.get_by_token_id(token_id)
                        if market is None:
                            continue
                        cost = paper_ledger.cost_basis_usdc.get(token_id, Decimal("0"))
                        # 跟踪 per-token max_unrealized_loss（drawdown 时序统计指标）
                        ob = market_ws_worker.snapshot(token_id)
                        if ob is not None and ob.best_bid is not None and ob.sell_actionable:
                            paper_ledger.observe_unrealized(token_id, ob.best_bid)
                        paper_positions.append(Position(
                            strategy_id=strategy_id,
                            condition_id=market.condition_id,
                            token_id=token_id,
                            market_slug=market.market_slug,
                            shares=shares,
                            cost_usdc=cost,
                        ))
                    account_state_store.replace_positions(tuple(paper_positions))
                    _SPM.get().worker_tick("paper_balance_syncer", expected_interval_s=1.0)
                except Exception:
                    pass
                await asyncio.sleep(1)

        paper_balance_syncer_task = asyncio.create_task(  # noqa: F841 - 保留引用防 GC
            _paper_balance_syncer(), name="paper_balance_syncer"
        )

        # 资金曲线时序记录器：每 60s 写一条 equity_snapshot 到 paper_ledger，
        # 供 /runtime/equity-curve 画图。in-memory，重启清零。最多保留 1440 条 (24h)。
        async def _equity_curve_recorder() -> None:
            from datetime import datetime, timezone
            while True:
                try:
                    total_cost = Decimal("0")
                    unrealized_value = Decimal("0")
                    for tok, shares in paper_ledger.positions.items():
                        cost = paper_ledger.cost_basis_usdc.get(tok, Decimal("0"))
                        total_cost += cost
                        ob = market_ws_worker.snapshot(tok)
                        if ob and ob.best_bid is not None and ob.sell_actionable:
                            unrealized_value += shares * ob.best_bid
                    equity = paper_ledger.available_usdc + unrealized_value
                    if not hasattr(paper_ledger, "equity_curve"):
                        paper_ledger.equity_curve = []  # type: ignore[attr-defined]
                    paper_ledger.equity_curve.append({  # type: ignore[attr-defined]
                        "at": datetime.now(timezone.utc).isoformat(),
                        "available_usdc": str(paper_ledger.available_usdc),
                        "total_cost_usdc": str(total_cost),
                        "unrealized_value_usdc": str(unrealized_value),
                        "equity_usdc": str(equity),
                        "positions_count": len(paper_ledger.positions),
                        "fees_accrued_usdc": str(paper_ledger.fees_accrued_usdc),
                    })
                    # 30s 一次（720 点 = 6h，max 2880 点 = 24h）
                    if len(paper_ledger.equity_curve) > 2880:  # type: ignore[attr-defined]
                        paper_ledger.equity_curve = paper_ledger.equity_curve[-2880:]  # type: ignore[attr-defined]
                    _SPM.get().worker_tick("equity_curve_recorder", expected_interval_s=30.0)
                except Exception:
                    pass
                await asyncio.sleep(30)
        equity_curve_task = asyncio.create_task(_equity_curve_recorder(), name="paper_equity_curve")  # noqa: F841 - 保留引用防 GC

        # 系统性能采样后台任务（每 60s 采 RSS + 每 30s 采 PnL）
        async def _system_perf_sampler() -> None:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            import resource
            import platform
            mon = SystemPerfMonitor.get()
            while True:
                # 用 Python 内置 resource 模块（无 psutil 依赖）
                try:
                    ru = resource.getrusage(resource.RUSAGE_SELF)
                    rss_bytes = ru.ru_maxrss if platform.system() == "Darwin" else ru.ru_maxrss * 1024
                    mon.sample_memory(rss_bytes / 1024 / 1024)
                except Exception:
                    pass
                # PnL: equity = avail + unrealized_value（用 equity_curve 最新点）
                try:
                    if hasattr(paper_ledger, "equity_curve") and paper_ledger.equity_curve:
                        last = paper_ledger.equity_curve[-1]
                        pnl = float(last["equity_usdc"]) - float(settings.portfolio_budget_usdc)
                        mon.sample_pnl(pnl)
                except Exception:
                    pass
                await asyncio.sleep(30)
        system_perf_sampler_task = asyncio.create_task(_system_perf_sampler(), name="system_perf_sampler")  # noqa: F841 - 保留引用防 GC

        # event loop scheduler lag prober:每 2s await sleep(0.1),实际耗时 - 100 ms = loop 被卡多久
        async def _eventloop_lag_prober() -> None:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            mon = SystemPerfMonitor.get()
            target_sleep_s = 0.1
            while True:
                t0 = time.perf_counter()
                await asyncio.sleep(target_sleep_s)
                actual_ms = (time.perf_counter() - t0) * 1000
                lag_ms = actual_ms - target_sleep_s * 1000
                mon.record_eventloop_lag(max(0.0, lag_ms))
                # 单次 lag > 50ms → 记 slow_callback(说明 loop 被某个 callback 长时间占用)
                if lag_ms > 50:
                    mon.record_slow_callback(lag_ms, task_name="eventloop_lag_prober_observed")
                await asyncio.sleep(2.0)
        eventloop_lag_task = asyncio.create_task(_eventloop_lag_prober(), name="eventloop_lag_prober")  # noqa: F841 - 保留引用防 GC
        # asyncio.loop.slow_callback_duration:默认 0.1s,超过会 warning.我们设 0.05
        # 让 loop 自己检测+log,我们的 prober 兜底.
        try:
            asyncio.get_event_loop().slow_callback_duration = 0.05
        except Exception:
            pass

        # GoalserveLazy 提供 schedule / h2h 等按需拉取（不轮询，仅 lookup 时调用）。
        api_key_secret = settings.goalserve_api_key
        api_key = api_key_secret.get_secret_value() if api_key_secret else None
        if api_key:
            goalserve_lazy_client = GoalserveLazyClient(
                api_key=api_key,
                proxy=settings.goalserve_proxy,
                cache_ttl_s=3600.0,
            )
            logger.info("GoalserveLazy (schedule/h2h) started")
        logger.warning(
            "paper_trading_mode=true → PaperSubmitOnlyOrderClient + 本地签名 + 虚拟余额 %s USDC。"
            "WS 盘口=market_ws_worker.snapshot, syncer 每秒同步 paper_ledger → account_state_store",
            settings.portfolio_budget_usdc,
        )
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
    market_service = MarketService(
        strategy=extension.hooks,
        registry=registry,
        market_tracker=market_ws_worker,
        account_snapshot_provider=account_state_store.snapshot,
        # 300s(5min) audit 节流:discovery 每秒扫 22 个新 cid,1h 累 420 MB audit;
        # 5min 颗粒度对复盘"为什么这个市场被拒"足够,节省 80% audit 写入.
        filter_emit_min_interval_s=300.0,
    )
    decision_recorder = DecisionEventRecorder(outbox=outbox, strategy_id=strategy_id)
    trading_decision_service = TradingDecisionService(
        strategy=extension.hooks,
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

    _exposure_classifier = extension

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
        entry_metadata_provider=entry_metadata_for_event,
        orderbook_direction_signal_reader=orderbook_delta_store.direction_signal,
        parameter_store=parameter_store,
    )
    reconcile_service = ReconcileService(
        strategy=extension.hooks,
        strategy_id=strategy_id,
        entry_metadata_provider=entry_metadata_for_market,
        orderbook_reader=trading_decision_service.lookup_orderbook,
    )
    # 注册 trading_decision_worker prune callback:market prune 时同步清 worker
    # 内部 4 个 cid/token 索引 dict(_market_lifecycle / _token_*) 防内存泄漏.
    registry.register_prune_callback(trading_decision_worker.evict_market)
    # entry_metadata_store 已有 remove API,适配成 callback signature 注册:
    registry.register_prune_callback(
        lambda cid, _tokens: entry_metadata_store.remove(condition_id=cid)
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
        paper_mode=settings.paper_trading_mode,
        gamma_snapshot_store=gamma_snapshot_store,
        # 用户态拉取已经交给独立 UserAccountPoller，reconcile 主循环不再内联 fetch
        # → 避免 data API / clob balance 网络抖动牵连 market 元数据刷新链路。
        refresh_account_inline=False,
    )
    # 注册 authority_refresher prune callback:market prune 时清 _condition_failure_counts.
    registry.register_prune_callback(reconcile_worker._authority_refresher.evict_market)
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
                extra={"extension": "polymarket_trader.quant"},
            )
        else:
            league_source_priority = live_state_hooks.league_source_affinity
            sports_live_state_client = _build_sports_live_state_client(
                settings,
                league_source_priority=league_source_priority,
                trusted_sources=None,  # 默认走 aggregate 内置 _DEFAULT_OFFICIAL_SOURCES
                livescore_active_sports_provider=(
                    _build_livescore_active_sports_provider(registry)
                ),
                inplay_active_sports_provider=(
                    _build_inplay_active_sports_provider(registry)
                ),
            )
            sports_live_state_worker = SportsLiveStateWorker(
                snapshot_provider=sports_live_state_client.list_events,
                match_live_state=live_state_hooks.match_live_state,
                registry=registry,
                entry_metadata_store=entry_metadata_store,
                event_bus=event_bus,
                market_tracker=market_ws_worker.track_market,
                lifecycle_bus=lifecycle_bus,
                market_pauser=account_state_store,
                enabled=True,
                source="sports_live_aggregate",
                leagues=settings.sports_live_state_league_codes,
                publish_entry_signals=settings.sports_live_state_publish_entry_signals,
                # 60s audit 节流:state_hash dedupe 因 live_game 嵌套时间字段
                # (seconds_remaining 等)每 5s 都变而失效.60s 颗粒度对复盘足够,
                # P0 决策走内存不依赖 audit.30s→60s 省 50% sports_live_state audit.
                audit_min_interval_s=60.0,
            )
    season_state_store = SeasonStateStore()
    bind_extension_season_state(runtime_ports, season_state_store)
    # 注：ESPN-based season-state + series-state worker 已经删除（只服务传统
    # 体育，对当前 e-sports 100% 浪费）。store 保留，strategy ports 仍可绑，
    # 没有 writer 等于空 store——strategy 读到 None 时门控自然跳过。
    season_odds_worker, season_odds_client = _build_season_odds_worker(
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
    # 注册 odds workers + user_ws 的 prune callbacks
    if season_odds_worker is not None:
        registry.register_prune_callback(season_odds_worker.evict_market)
    if game_odds_worker is not None:
        registry.register_prune_callback(game_odds_worker.evict_market)
    registry.register_prune_callback(user_ws_worker.evict_market)
    # account_state 的 _fills 也按 market lifecycle 清(fills 已写 DB,内存不必常驻 dead market).
    registry.register_prune_callback(account_state_store.evict_market)
    pregame_worker, pregame_client = _build_pregame_worker(
        settings,
        registry=registry,
        entry_metadata_store=entry_metadata_store,
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
        gamma_snapshot_store=gamma_snapshot_store,
        outbox=outbox,
        db_engine=db_engine,
        db_session_factory=db_session_factory,
        persistence_repository=persistence_repository,
        persistence_worker=persistence_worker,
        account_state_store=account_state_store,
        entry_metadata_store=entry_metadata_store,
        order_executor=order_executor,
        market_ws_worker=market_ws_worker,
        orderbook_delta_store=orderbook_delta_store,
        orderbook_history_buffer=orderbook_history_buffer,
        orderbook_derived_store=orderbook_derived_store,
        orderbook_derived_publisher=orderbook_derived_publisher,
        user_ws_worker=user_ws_worker,
        market_service=market_service,
        market_discovery_worker=market_discovery_worker,
        market_discovery_scan=market_discovery_scan,
        sports_live_state_client=sports_live_state_client,
        sports_live_state_worker=sports_live_state_worker,
        season_state_store=season_state_store,
        season_odds_worker=season_odds_worker,
        season_odds_client=season_odds_client,
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
        paper_ledger=paper_ledger if settings.paper_trading_mode else None,
        goalserve_lazy_client=goalserve_lazy_client,
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
    from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
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

    with suppress(Exception):
        await runtime.trading_service.aclose()
    with suppress(Exception):
        await runtime.order_executor.aclose()
    for client in (runtime.gamma_client, runtime.clob_client, runtime.data_client):
        with suppress(Exception):
            await client.aclose()
    if runtime.sports_live_state_client is not None:
        with suppress(Exception):
            await runtime.sports_live_state_client.aclose()
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
    runtime.supervisor.register_worker("admin_api", priority="P3", state=WorkerLifecycleState.RUNNING)
    runtime.supervisor.register_worker("market_discovery", priority="P2")
    runtime.supervisor.register_worker("market_ws", priority="P0", state=WorkerLifecycleState.PAUSED)
    runtime.supervisor.register_worker("user_ws", priority="P0", state=WorkerLifecycleState.PAUSED)
    runtime.supervisor.register_worker("trading_decision", priority="P0")
    runtime.supervisor.register_worker("reconcile", priority="P2")
    if runtime.sports_live_state_worker is not None:
        runtime.supervisor.register_worker("sports_live_state_sync", priority="P2")
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
    import asyncio as _asyncio
    await _asyncio.gather(*[_one() for _ in range(n)], return_exceptions=True)


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
        "position_exit_evaluator",
        lambda: _publish_position_exit_evaluator_tick(runtime),
        priority="P1",
        interval_seconds=5.0,
        tags=("position", "exit"),
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


async def _publish_position_exit_evaluator_tick(runtime: RuntimeComponents) -> None:
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
                reason="position_exit_evaluator_tick",
                payload={"source": "position_exit_evaluator", "synthetic": True},
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

    from polymarket_trader.app.dead_records_retention import purge_dead_records_once

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
    """运行一次结算扫描。任何异常仅记日志——不阻塞 supervisor。

    §3 强化版：scanner 不访问 DB（DB 仅审计）。
    持仓来源 = AccountStateStore 内存快照（reconcile 已经从 Polymarket data API
    拉到内存）；幂等去重靠 service 内 in-process 集合 + outbox event_id
    (settlement:{cid}) 兜底，不再查 audit_events 表。
    """

    from polymarket_trader.app.settlement_scanner import SettlementScannerService

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
