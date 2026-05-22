from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING, Any, Callable, Mapping
from uuid import uuid4

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.extension_api import DiscoveryQuery

if TYPE_CHECKING:
    from polymarket_trader.main import RuntimeComponents

_MARKET_DISCOVERY_EVENT_PAGE_LIMIT = 50
_MARKET_DISCOVERY_MARKET_BUDGET_PER_TICK = 1000
_MARKET_DISCOVERY_REQUEST_BUDGET_PER_TICK = 2
_MARKET_DISCOVERY_MAX_RUNTIME_MS = 200.0
_LIVE_EVENT_EXPANSION_BUDGET_PER_TICK = 1
_LIVE_EVENT_EXPANSION_REFRESH_SECONDS = 30.0
MARKET_DISCOVERY_TICK_SECONDS = 0.5
# P3.1：持仓/挂单市场独立快速刷新间隔。常规 Gamma 全量轮转可能几分钟才回到某个
# condition_id；有持仓的市场每 15s 单独拉一次，确保盘口状态不滞后。
_PRIORITY_CONDITION_REFRESH_SECONDS = 15.0
# 每 tick 最多为 priority 市场额外发出 1 次 gamma 请求，避免挤占常规发现预算。
_PRIORITY_CONDITION_REFRESH_BUDGET_PER_TICK = 1
# 单次失败的基础回退，下次重试至少等这么久。
MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS = 5
# 指数回退上限：5 → 10 → 20 → 40 → 60s 后封顶。tail 策略对发现实时性要求高，
# 一次 gamma 超时不应让我们 60s 无新市场；同时避免长期故障打爆 gamma API。
MARKET_DISCOVERY_RETRY_BACKOFF_MAX_SECONDS = 60


def _retry_backoff_seconds(consecutive_failures: int) -> int:
    """指数回退：5/10/20/40/60s，最多翻倍 4 次后封顶。"""

    exponent = max(0, consecutive_failures - 1)
    if exponent > 4:
        exponent = 4
    backoff = MARKET_DISCOVERY_RETRY_BACKOFF_SECONDS * (2 ** exponent)
    return min(backoff, MARKET_DISCOVERY_RETRY_BACKOFF_MAX_SECONDS)

logger = logging.getLogger(__name__)
RuntimeMetricsSync = Callable[[Any], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sync(sync_runtime_metrics: RuntimeMetricsSync | None, runtime: RuntimeComponents) -> None:
    if sync_runtime_metrics is not None:
        sync_runtime_metrics(runtime)


@dataclass(slots=True)
class FullMarketDiscoveryState:
    after_cursor: str | None = None
    query_cursors: dict[str, str] = field(default_factory=dict)
    completed_query_names: set[str] = field(default_factory=set)
    next_query_index: int = 0
    round_id: int = 1
    round_started_at: datetime | None = None
    last_round_completed_at: datetime | None = None
    last_completed_round_pages: int = 0
    last_completed_round_markets: int = 0
    last_page_size: int = 0
    pages_scanned_in_round: int = 0
    markets_seen_in_round: int = 0
    last_tick_started_at: datetime | None = None
    last_tick_completed_at: datetime | None = None
    last_tick_requests: int = 0
    last_tick_markets: int = 0
    last_error: str | None = None
    consecutive_failures: int = 0
    last_query_names: tuple[str, ...] = ()
    live_event_expanded_at: dict[str, datetime] = field(default_factory=dict)
    # P3.1：记录各 priority condition_id 上次单独刷新的时间，避免重复请求。
    priority_condition_refreshed_at: dict[str, datetime] = field(default_factory=dict)

    def start_tick(self) -> None:
        self.last_tick_started_at = _utc_now()
        self.last_tick_completed_at = None
        self.last_tick_requests = 0
        self.last_tick_markets = 0
        if self.round_started_at is None:
            self.round_started_at = self.last_tick_started_at

    def next_query(self, queries: tuple[DiscoveryQuery, ...]) -> DiscoveryQuery | None:
        query_name_sequence = tuple(query.name for query in queries)
        if query_name_sequence != self.last_query_names:
            self.next_query_index = 0
            self.last_query_names = query_name_sequence
        query_names = {query.name for query in queries}
        self.query_cursors = {
            name: cursor for name, cursor in self.query_cursors.items() if name in query_names
        }
        self.completed_query_names = {
            name for name in self.completed_query_names if name in query_names
        }
        if not queries or len(self.completed_query_names) >= len(query_names):
            return None
        for offset in range(len(queries)):
            index = (self.next_query_index + offset) % len(queries)
            query = queries[index]
            if query.name in self.completed_query_names:
                continue
            self.next_query_index = (index + 1) % len(queries)
            self.after_cursor = self.query_cursors.get(query.name)
            return query
        return None

    def record_page(
        self,
        *,
        query_name: str = "default",
        total_queries: int = 1,
        page_size: int,
        next_cursor: str | None,
    ) -> bool:
        self.last_page_size = max(0, int(page_size))
        self.pages_scanned_in_round += 1
        self.markets_seen_in_round += max(0, int(page_size))
        self.last_tick_requests += 1
        self.last_tick_markets += max(0, int(page_size))
        if next_cursor is None:
            self.query_cursors.pop(query_name, None)
            self.completed_query_names.add(query_name)
        else:
            self.query_cursors[query_name] = next_cursor
        self.after_cursor = next_cursor or next(iter(self.query_cursors.values()), None)
        self.last_error = None
        # 单页成功不清零 consecutive_failures：一轮 discovery 跨多次失败/恢复时，
        # 中间夹一次 success 会让指数回退计数器永远停在 1（永远只回退 5s），
        # 实际从未升到 10/20/40/60s。只在 finish_round 才认为整轮稳定，重置计数（F5）。
        return len(self.completed_query_names) >= max(1, int(total_queries))

    def finish_round(self) -> None:
        self.after_cursor = None
        self.query_cursors.clear()
        self.completed_query_names.clear()
        self.next_query_index = 0
        self.last_completed_round_pages = self.pages_scanned_in_round
        self.last_completed_round_markets = self.markets_seen_in_round
        self.last_round_completed_at = _utc_now()
        self.round_id += 1
        self.round_started_at = None
        self.pages_scanned_in_round = 0
        self.markets_seen_in_round = 0
        self.consecutive_failures = 0

    def finish_tick(self) -> None:
        self.last_tick_completed_at = _utc_now()

    def record_failure(self, reason: str) -> None:
        self.last_error = reason
        self.consecutive_failures += 1
        self.last_tick_completed_at = _utc_now()


async def run_market_discovery_scan(
    runtime: RuntimeComponents,
    *,
    sync_runtime_metrics: RuntimeMetricsSync | None = None,
) -> None:
    state = runtime.market_discovery_scan
    if (
        runtime.market_discovery_worker.last_failure is not None
        and not runtime.market_discovery_worker.should_retry()
    ):
        retry_at = runtime.market_discovery_worker.last_failure.retry_at.isoformat()
        runtime.supervisor.heartbeat_worker(
            "market_discovery",
            detail=f"retry_backoff until={retry_at}",
        )
        _sync(sync_runtime_metrics, runtime)
        return

    state.start_tick()
    runtime.supervisor.heartbeat_worker(
        "market_discovery",
        detail=(
            f"round={state.round_id} cursor={'set' if state.after_cursor else 'start'} "
            f"budget={_MARKET_DISCOVERY_REQUEST_BUDGET_PER_TICK}"
        ),
    )
    started_at = asyncio.get_running_loop().time()
    try:
        queries = _discovery_queries(runtime)
        round_completed = False
        completed_round_id: int | None = None
        completed_round_pages = 0
        completed_round_markets = 0
        while (
            state.last_tick_requests < _MARKET_DISCOVERY_REQUEST_BUDGET_PER_TICK
            and state.last_tick_markets < _MARKET_DISCOVERY_MARKET_BUDGET_PER_TICK
        ):
            elapsed_ms = (asyncio.get_running_loop().time() - started_at) * 1000.0
            if elapsed_ms >= _MARKET_DISCOVERY_MAX_RUNTIME_MS:
                break
            query = state.next_query(queries)
            if query is None:
                round_completed = True
                completed_round_id = state.round_id
                completed_round_pages = state.pages_scanned_in_round
                completed_round_markets = state.markets_seen_in_round
                state.finish_round()
                runtime.metrics.inc_counter("market_discovery_rounds_completed_total", 1.0)
                break
            page_markets, next_cursor = await fetch_full_market_discovery_page(runtime, query=query)
            runtime.market_discovery_worker.mark_scan_success()
            market_payloads = tuple(raw_event.payload for raw_event in page_markets)
            query_completed_round = state.record_page(
                query_name=query.name,
                total_queries=len(queries),
                page_size=len(market_payloads),
                next_cursor=next_cursor,
            )
            runtime.metrics.inc_counter("market_discovery_requests_total", 1.0)
            runtime.metrics.inc_counter("market_discovery_pages_scanned_total", 1.0)
            if market_payloads:
                runtime.metrics.inc_counter(
                    "market_discovery_markets_scanned_total",
                    float(len(market_payloads)),
                )

            await runtime.market_discovery_worker.ingest_source_page(
                {"markets": list(market_payloads)},
                source="gamma.events_keyset",
                trace_id=f"market-discovery-round-{state.round_id}-{uuid4().hex}",
            )
            if query_completed_round:
                round_completed = True
                completed_round_id = state.round_id
                completed_round_pages = state.pages_scanned_in_round
                completed_round_markets = state.markets_seen_in_round
                state.finish_round()
                runtime.metrics.inc_counter("market_discovery_rounds_completed_total", 1.0)
                break
            if state.last_tick_markets >= _MARKET_DISCOVERY_MARKET_BUDGET_PER_TICK:
                break
        await expand_live_event_market_discovery(runtime)
        await refresh_priority_condition_ids(runtime)
    except Exception as exc:
        state.record_failure(str(exc))
        backoff_seconds = _retry_backoff_seconds(state.consecutive_failures)
        runtime.market_discovery_worker.record_failure(
            source="gamma.events_keyset",
            reason=str(exc),
            retry_after_seconds=backoff_seconds,
        )
        runtime.supervisor.mark_worker_error(
            "market_discovery",
            detail=f"discover_failed retry_in={backoff_seconds}s fail_n={state.consecutive_failures}",
            last_error=str(exc),
        )
        logger.warning(
            "market discovery scan failed",
            extra={
                "reason": str(exc),
                "consecutive_failures": state.consecutive_failures,
                "retry_after_seconds": backoff_seconds,
            },
        )
        # 连续失败时在本次调用内也主动等待，避免紧循环打爆 gamma API。
        # 调度器层已通过 should_retry() 时间门控；这里的 sleep 是额外保险，
        # 确保即使调度间隔极短，连续失败也能得到指数回退缓冲。
        if state.consecutive_failures > 0:
            await asyncio.sleep(min(5.0 * (2 ** (state.consecutive_failures - 1)), 60.0))
    else:
        state.finish_tick()
        if round_completed and completed_round_id is not None:
            runtime.supervisor.heartbeat_worker(
                "market_discovery",
                detail=(
                    f"round_completed={completed_round_id} "
                    f"pages={completed_round_pages} markets={completed_round_markets}"
                ),
            )
        else:
            runtime.supervisor.heartbeat_worker(
                "market_discovery",
                detail=(
                    f"round={state.round_id} reqs={state.last_tick_requests} "
                    f"markets={state.last_tick_markets} cursor={'set' if state.after_cursor else 'start'}"
                ),
            )
    finally:
        _sync(sync_runtime_metrics, runtime)


async def expand_live_event_market_discovery(runtime: RuntimeComponents) -> None:
    """对已匹配直播事件按 event slug 精确补齐同场子盘口。

    常规 title_search discovery 会受查询轮转和分页预算影响；实盘中一旦
    live state 已确认某个 event，就应快速拉齐该事件的全场、分盘和总分盘口。
    """

    state = runtime.market_discovery_scan
    slugs = _live_event_slugs_for_expansion(runtime, now=_utc_now())
    for event_slug in slugs[:_LIVE_EVENT_EXPANSION_BUDGET_PER_TICK]:
        events = await runtime.gamma_client.list_events(
            active=True,
            closed=False,
            slug=event_slug,
            limit=5,
            timeout_s=2.0,
        )
        raw_events: list[Any] = []
        for event in events:
            raw_events.extend(event.to_raw_market_events(source="gamma.events_slug"))
        if raw_events:
            await runtime.market_discovery_worker.ingest_source_page(
                {"markets": [raw_event.payload for raw_event in raw_events]},
                source="gamma.events_slug",
                trace_id=f"market-discovery-live-event-{uuid4().hex}",
            )
        state.live_event_expanded_at[event_slug] = _utc_now()


async def refresh_priority_condition_ids(runtime: RuntimeComponents) -> None:
    """P3.1：对持仓/挂单市场按独立快速间隔（15s）单独拉取 Gamma 市场快照。

    常规全量 Gamma 轮转可能数分钟才回到某个 condition_id；有持仓的市场需要
    更频繁的盘口状态更新，确保 TradingDecisionWorker 读到的 registry 不滞后。
    每 tick 最多发 1 次额外 gamma 请求，不挤占常规发现预算。
    """

    state = runtime.market_discovery_scan
    condition_ids = _priority_condition_ids_due_for_refresh(runtime, now=_utc_now())
    if not condition_ids:
        return
    gamma_client = runtime.gamma_client
    if gamma_client is None:
        return
    fetched = 0
    for condition_id in condition_ids:
        if fetched >= _PRIORITY_CONDITION_REFRESH_BUDGET_PER_TICK:
            break
        try:
            market_dto = await gamma_client.get_market(condition_id, timeout_s=2.0)
        except Exception as exc:
            logger.debug(
                "priority_condition_refresh failed",
                extra={"condition_id": condition_id, "reason": str(exc)},
            )
            # 单次失败不中断其他 priority 市场的刷新，也不更新 refreshed_at，
            # 下次 tick 会自然重试。
            continue
        await runtime.market_discovery_worker.ingest_source_page(
            {"markets": [dict(market_dto.raw)]},
            source="gamma.priority_refresh",
            trace_id=f"priority-refresh-{condition_id[:8]}-{uuid4().hex}",
        )
        state.priority_condition_refreshed_at[condition_id] = _utc_now()
        fetched += 1


def _priority_condition_ids_due_for_refresh(runtime: RuntimeComponents, *, now: datetime) -> tuple[str, ...]:
    """从账户持仓/挂单中提取需要快速刷新的 condition_id 集合。"""

    account_snapshot = _account_snapshot_for_discovery(runtime)
    if account_snapshot is None:
        return ()
    state = runtime.market_discovery_scan
    due: list[str] = []
    for position in account_snapshot.positions:
        if position.settled_zero_value:
            continue
        condition_id = position.condition_id
        if not condition_id:
            continue
        refreshed_at = state.priority_condition_refreshed_at.get(condition_id)
        if refreshed_at is not None and (now - refreshed_at).total_seconds() < _PRIORITY_CONDITION_REFRESH_SECONDS:
            continue
        due.append(condition_id)
    for order in account_snapshot.open_orders:
        condition_id = order.condition_id
        if not condition_id:
            continue
        refreshed_at = state.priority_condition_refreshed_at.get(condition_id)
        if refreshed_at is not None and (now - refreshed_at).total_seconds() < _PRIORITY_CONDITION_REFRESH_SECONDS:
            continue
        due.append(condition_id)
    return tuple(dict.fromkeys(due))


def _account_snapshot_for_discovery(runtime: RuntimeComponents) -> AccountSnapshot | None:
    return runtime.account_state_store.snapshot()


def _live_event_slugs_for_expansion(runtime: RuntimeComponents, *, now: datetime) -> tuple[str, ...]:
    store = runtime.entry_metadata_store
    state = runtime.market_discovery_scan
    slugs: list[str] = []
    for record in store.records():
        event_slug = (record.event_slug or "").strip()
        if not event_slug:
            continue
        phase = (record.live_state_phase or "").strip().lower()
        if phase not in {"live", "ended"}:
            continue
        expanded_at = state.live_event_expanded_at.get(event_slug)
        if expanded_at is not None and (now - expanded_at).total_seconds() < _LIVE_EVENT_EXPANSION_REFRESH_SECONDS:
            continue
        slugs.append(event_slug)
    return tuple(dict.fromkeys(slugs))


async def fetch_full_market_discovery_page(
    runtime: RuntimeComponents,
    *,
    query: DiscoveryQuery | None = None,
) -> tuple[tuple[Any, ...], str | None]:
    state = runtime.market_discovery_scan
    query = query or DEFAULT_DISCOVERY_QUERY
    params: dict[str, Any] = {
        "active": True,
        "closed": False,
        # 只扫近期创建的 event——把数月前创建的古老期货/赛季盘挡在 gamma 查询
        # 之外，大幅减少扫描页数。体育单场赛事的 event 通常在赛前数天~数周创建，
        # 30 天余量足以覆盖今天的比赛。
        "start_date_min": _market_discovery_start_date_min(),
    }
    params.update(_safe_query_params(query.params))
    params["limit"] = _MARKET_DISCOVERY_EVENT_PAGE_LIMIT
    after_cursor = state.query_cursors.get(query.name) or state.after_cursor
    if after_cursor is not None:
        params["after_cursor"] = after_cursor
    events, next_cursor = await runtime.gamma_client.list_events_keyset_by_params(params, timeout_s=2.0)
    raw_events: list[Any] = []
    for event in events:
        raw_events.extend(event.to_raw_market_events(source="gamma.events_keyset"))
    return tuple(raw_events), next_cursor


DEFAULT_DISCOVERY_QUERY = DiscoveryQuery()
_FRAMEWORK_DISCOVERY_PARAM_KEYS = {"limit", "after_cursor"}
# discovery gamma 扫描只回溯 30 天内创建的 event——更早创建的多为已结束赛事
# 与长期期货盘，不需要重新扫描。
_MARKET_DISCOVERY_CREATED_LOOKBACK = timedelta(days=30)


def _market_discovery_start_date_min() -> str:
    """gamma 扫描的 start_date_min 参数（当前时间回溯 30 天，UTC ISO8601）。"""
    return (datetime.now(timezone.utc) - _MARKET_DISCOVERY_CREATED_LOOKBACK).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _discovery_queries(runtime: RuntimeComponents) -> tuple[DiscoveryQuery, ...]:
    hooks = runtime.extension.hooks
    queries = (*_live_game_discovery_queries(runtime, hooks), *_configured_discovery_queries(hooks))
    return _dedupe_discovery_queries(queries) or (DEFAULT_DISCOVERY_QUERY,)


def _configured_discovery_queries(hooks: Any) -> tuple[DiscoveryQuery, ...]:
    method = getattr(hooks, "discovery_queries", None)
    if not callable(method):
        return (DEFAULT_DISCOVERY_QUERY,)
    queries = tuple(query for query in method() if isinstance(query, DiscoveryQuery) and query.name.strip())
    return queries or (DEFAULT_DISCOVERY_QUERY,)


def _live_game_discovery_queries(runtime: RuntimeComponents, hooks: Any) -> tuple[DiscoveryQuery, ...]:
    """从直播状态 worker 的最近比赛快照中提取策略高意图查询。

    ``hooks`` 这里没用——live state 相关 hook 在 ``extension.live_state_hooks``，
    策略未实现时直接跳过；framework 不再向核心 ExtensionHooks 强制 live state 接口。
    """

    worker = runtime.sports_live_state_worker
    if worker is None:
        return ()
    events = tuple(worker.last_events())
    if not events:
        return ()
    live_state_hooks = runtime.extension.live_state_hooks
    if live_state_hooks is None:
        return ()
    return tuple(
        query
        for query in live_state_hooks.discovery_queries_for_live_events(events)
        if isinstance(query, DiscoveryQuery) and query.name.strip()
    )


def _dedupe_discovery_queries(queries: tuple[DiscoveryQuery, ...]) -> tuple[DiscoveryQuery, ...]:
    """按查询参数去重，保持直播高意图查询优先。"""

    result: list[DiscoveryQuery] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for query in queries:
        key = tuple(sorted((str(name), str(value)) for name, value in query.params.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(query)
    return tuple(result)


def _safe_query_params(params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in params.items()
        if str(key) not in _FRAMEWORK_DISCOVERY_PARAM_KEYS
    }
