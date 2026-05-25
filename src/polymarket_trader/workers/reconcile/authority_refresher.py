from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol, TypeVar

from polymarket_trader.app.order_projection import AccountStateProjector
from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import OrderRecord
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.gamma_snapshot_store import GammaMarketSnapshotStore
from polymarket_trader.runtime.registry import MarketRegistry, MarketRegistrySnapshot
from polymarket_trader.workers.market_ws import MarketWsWorker

RegistrySnapshotProvider = Callable[[], MarketRegistrySnapshot]
_AUTHORITY_CALL_TIMEOUT_S = 5.0
# gamma 元数据快照新鲜度阈值。discovery 每 0.5s 刷一次 events.markets，所以
# 30s 内的 entry 一定是 discovery 写入的，跳过 /markets 直查。stale 或 missing
# 都走 fallback 路径——保证孤儿 condition / 已下架市场仍能被检测到。
_GAMMA_SNAPSHOT_FRESH_WINDOW_S = 30.0
# 市场 WS orderbook 新鲜度阈值。market_ws 实时推送 + gap repair 已经覆盖正常
# 链路，reconcile 只在 WS 长时间没推送时做 REST 兜底（liveness probe）。
# 30s 是保守阈值——WS 通常每秒至少一条；30s 没推说明真断了。
_WS_ORDERBOOK_FRESH_WINDOW_S = 30.0
_T = TypeVar("_T")


class MarketAuthorityClient(Protocol):
    async def list_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        tag: str | None = None,
        slug: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout_s: float | None = None,
    ) -> tuple[GammaMarketCandidate, ...]: ...

    async def get_market_by_condition_id(
        self, condition_id: str, *, timeout_s: float | None = None
    ) -> GammaMarketCandidate | None: ...


class OrderAuthorityClient(Protocol):
    @property
    def has_auth_client(self) -> bool: ...

    async def get_orderbook(
        self,
        token_id: str,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
    ) -> Any: ...

    async def get_fee_rate(self, token_id: str) -> int | None: ...

    async def list_open_orders(self) -> tuple[Any, ...]: ...

    async def list_fills(self) -> tuple[Any, ...]: ...

    async def get_balance_allowance(self) -> Any: ...


class DataAuthorityClient(Protocol):
    @property
    def has_auth_client(self) -> bool: ...

    async def list_positions(self) -> tuple[Any, ...]: ...


class GammaMarketCandidate(Protocol):
    condition_id: str | None
    market_slug: str | None
    clob_enabled: bool | None
    outcomes: tuple[MarketOutcome, ...]

    def to_market(self) -> Market: ...


class TradingAuthorityClient(Protocol):
    pass


@dataclass(frozen=True, slots=True)
class AuthoritativeRefreshFailure:
    component: str
    operation: str
    target: str | None = None
    reason: str = "exception"
    detail: str = ""
    retryable: bool = True

    def as_payload(self) -> dict[str, object]:
        return {
            "component": self.component,
            "operation": self.operation,
            "target": self.target,
            "reason": self.reason,
            "detail": self.detail,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeMarketRefresh:
    requested_market: Market
    refreshed_market: Market | None
    fee_rate_refreshed: bool = False
    failures: tuple[AuthoritativeRefreshFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthoritativeRefreshSummary:
    trace_id: str
    market_count: int
    refreshed_markets: int
    refreshed_orderbooks: int
    refreshed_fee_rates: int
    refreshed_positions: int
    refreshed_open_orders: int
    refreshed_fills: int
    refreshed_balance: bool
    refreshed_allowance: bool
    user_refresh_enabled: bool
    failures: tuple[AuthoritativeRefreshFailure, ...] = ()

    def as_payload(self) -> dict[str, object]:
        return {
            "market_count": self.market_count,
            "refreshed_markets": self.refreshed_markets,
            "refreshed_orderbooks": self.refreshed_orderbooks,
            "refreshed_fee_rates": self.refreshed_fee_rates,
            "refreshed_positions": self.refreshed_positions,
            "refreshed_open_orders": self.refreshed_open_orders,
            "refreshed_fills": self.refreshed_fills,
            "refreshed_balance": self.refreshed_balance,
            "refreshed_allowance": self.refreshed_allowance,
            "user_refresh_enabled": self.user_refresh_enabled,
            "failures": [failure.as_payload() for failure in self.failures],
        }


class ReconcileAuthorityRefresher:
    def __init__(
        self,
        *,
        registry_snapshot_provider: RegistrySnapshotProvider | None = None,
        account_state_store: AccountStateStore | None = None,
        registry: MarketRegistry | None = None,
        market_ws_worker: MarketWsWorker | None = None,
        gamma_client: MarketAuthorityClient | None = None,
        clob_client: OrderAuthorityClient | None = None,
        data_client: DataAuthorityClient | None = None,
        trading_client: TradingAuthorityClient | None = None,
        authority_call_timeout_s: float | None = None,
        market_authority_concurrency: int = 8,
        paper_mode: bool = False,
        gamma_snapshot_store: GammaMarketSnapshotStore | None = None,
        refresh_account_inline: bool = True,
    ) -> None:
        self._registry_snapshot_provider = registry_snapshot_provider
        self._account_state_store = account_state_store
        self._registry = registry
        self._market_ws_worker = market_ws_worker
        self._gamma_client = gamma_client
        self._clob_client = clob_client
        self._data_client = data_client
        self._trading_client = trading_client
        # discovery_runner 每 0.5s 拉一次 /events?live=true，nested markets 已含
        # 完整 GammaMarketDTO；写入 gamma_snapshot_store 后，reconcile 这边 fetch
        # 命中缓存就直接返回，**完全省掉每 market 一次 /markets 网络调用**。
        # 缓存 miss 或 stale（>30s）才 fallback 走原来的 /markets?slug=...，覆盖
        # 孤儿 condition / 已下架市场。
        self._gamma_snapshot_store = gamma_snapshot_store
        # refresh() 主循环是否内联调用 refresh_account。当 UserAccountPoller 独立
        # 后台轮询用户态时设 False——reconcile 主链路只做 market 元数据刷新，不再
        # 同步等 data API / clob balance 网络调用。refresh_account 仍可被 poller
        # 直接调用，paper_mode 检查依然生效。
        self._refresh_account_inline = refresh_account_inline
        # paper 模式下 paper_ledger + paper_balance_syncer 是用户态唯一权威：
        # account_state_store 的 balance/positions/orders/fills 都由 syncer 每秒
        # 从 paper_ledger 投影回去。reconcile 这边再拉 Polymarket data API 会
        # 把"链上真实持仓"（用户钱包里的旧仓位）写回，与 syncer 抢同一份状态
        # → "时有时无的 24 个幽灵持仓"。paper 模式直接跳过 user-side fetch。
        self._paper_mode = paper_mode
        self._authority_call_timeout_s = (
            _AUTHORITY_CALL_TIMEOUT_S
            if authority_call_timeout_s is None
            else authority_call_timeout_s
        )
        self._market_authority_concurrency = max(1, market_authority_concurrency)
        # 自动 quarantine：condition_id → 连续 retryable 失败计数。每次 market
        # refresh 失败 +1，成功清零；超过阈值（默认 5）→ 自动 pause market 标
        # 'auto_quarantine_dead_market'，让 reconcile 下次跳过这个死 condition。
        # 防 5/23 比赛结算后 Polymarket 删 condition_id 时 reconcile 反复 retry
        # 把 refresh_summary.failures 撑到 56+ 触发 degraded warning。
        self._condition_failure_counts: dict[str, int] = {}
        self._quarantine_threshold = 5

    def evict_market(self, condition_id: str, token_ids: tuple[str, ...]) -> None:
        """registry prune callback:清 cid 的失败计数,避免 cid 永久累积."""
        self._condition_failure_counts.pop(condition_id, None)

    async def refresh(
        self,
        *,
        trace_id: str,
        condition_ids: tuple[str, ...] | None = None,
        refresh_market_authority: bool = True,
    ) -> AuthoritativeRefreshSummary:
        markets = self._target_markets(condition_ids=condition_ids)

        market_authority_targets = self._market_authority_targets(
            markets,
            condition_ids=condition_ids,
            refresh_market_authority=refresh_market_authority,
        )
        market_refreshes = await self._refresh_market_authority_targets(market_authority_targets)
        refresh_failures: list[AuthoritativeRefreshFailure] = []
        refreshed_markets = 0
        refreshed_orderbooks = 0
        refreshed_fee_rates = 0

        for item in market_refreshes:
            if isinstance(item, BaseException):
                refresh_failures.append(
                    AuthoritativeRefreshFailure(
                        component="reconcile",
                        operation="market_refresh",
                        reason="exception",
                        detail=str(item),
                    )
                )
                continue
            market_to_apply = item.refreshed_market
            if market_to_apply is None and item.requested_market.market_slug.startswith("account-exposure-"):
                market_to_apply = item.requested_market
            if market_to_apply is not None:
                refreshed_markets += 1
                self._apply_refreshed_market(market_to_apply)
            if item.fee_rate_refreshed:
                refreshed_fee_rates += 1
            refresh_failures.extend(item.failures)

        if self._refresh_account_inline:
            account_summary = await self.refresh_account(trace_id=trace_id, markets=markets)
            refresh_failures.extend(account_summary.failures)
        else:
            # UserAccountPoller 在跑——主循环跳过用户态拉取，不抢同一份 store。
            account_summary = AuthoritativeRefreshSummary(
                trace_id=trace_id,
                market_count=0,
                refreshed_markets=0,
                refreshed_orderbooks=0,
                refreshed_fee_rates=0,
                refreshed_positions=0,
                refreshed_open_orders=0,
                refreshed_fills=0,
                refreshed_balance=False,
                refreshed_allowance=False,
                user_refresh_enabled=False,
                failures=(),
            )

        missing_exposure_targets = self._missing_account_exposure_targets(
            existing_markets=markets,
            condition_ids=condition_ids,
        )
        missing_market_refreshes = await self._refresh_market_authority_targets(missing_exposure_targets)
        for item in missing_market_refreshes:
            if isinstance(item, BaseException):
                refresh_failures.append(
                    AuthoritativeRefreshFailure(
                        component="reconcile",
                        operation="account_exposure_market_refresh",
                        reason="exception",
                        detail=str(item),
                    )
                )
                continue
            market_to_apply = item.refreshed_market
            if market_to_apply is None and item.requested_market.market_slug.startswith("account-exposure-"):
                market_to_apply = item.requested_market
            if market_to_apply is not None:
                refreshed_markets += 1
                self._apply_refreshed_market(market_to_apply)
            if item.fee_rate_refreshed:
                refreshed_fee_rates += 1
            refresh_failures.extend(item.failures)

        return AuthoritativeRefreshSummary(
            trace_id=trace_id,
            market_count=len(markets) + len(missing_exposure_targets),
            refreshed_markets=refreshed_markets,
            refreshed_orderbooks=refreshed_orderbooks,
            refreshed_fee_rates=refreshed_fee_rates,
            refreshed_positions=account_summary.refreshed_positions,
            refreshed_open_orders=account_summary.refreshed_open_orders,
            refreshed_fills=account_summary.refreshed_fills,
            refreshed_balance=account_summary.refreshed_balance,
            refreshed_allowance=account_summary.refreshed_allowance,
            user_refresh_enabled=account_summary.user_refresh_enabled,
            failures=tuple(refresh_failures),
        )

    async def refresh_account(
        self,
        *,
        trace_id: str,
        markets: tuple[Market, ...],
    ) -> AuthoritativeRefreshSummary:
        failures: list[AuthoritativeRefreshFailure] = []
        data_refresh_enabled = self._data_client is not None and self._data_client.has_auth_client
        clob_refresh_enabled = self._clob_client is not None and self._clob_client.has_auth_client
        user_refresh_enabled = bool(self._trading_client is not None or data_refresh_enabled or clob_refresh_enabled)
        # paper 模式下 user-side 状态 = paper_ledger（由 paper_balance_syncer 投影），
        # reconcile 拉真链上余额/持仓只会 race-fight 这个 syncer。直接跳过。
        if self._paper_mode or not user_refresh_enabled:
            return AuthoritativeRefreshSummary(
                trace_id=trace_id,
                market_count=len(markets),
                refreshed_markets=0,
                refreshed_orderbooks=0,
                refreshed_fee_rates=0,
                refreshed_positions=0,
                refreshed_open_orders=0,
                refreshed_fills=0,
                refreshed_balance=False,
                refreshed_allowance=False,
                user_refresh_enabled=False,
                failures=(),
            )

        # 4 个 user-side 端点全部拉取（positions / orders / fills / balance）。
        # 虽然 user_ws 推送 orders + fills delta，但 Polymarket user_ws 不保证
        # 推送 initial state，REST 周期 + 冷启动覆盖 push 漏推 / 冷启动空白。
        # 频率由 user_account_poll cadence（默认 20s）控制，足够低保证不抢 WS
        # 决策链路带宽。再省的话需要确认 user_ws initial state 行为。
        positions_task = asyncio.create_task(self._fetch_positions(failures))
        open_orders_task = asyncio.create_task(self._fetch_open_orders(failures))
        fills_task = asyncio.create_task(self._fetch_fills(failures))
        balance_task = asyncio.create_task(self._fetch_balance(failures))
        positions, open_orders, fills, balance_result = await asyncio.gather(
            positions_task,
            open_orders_task,
            fills_task,
            balance_task,
        )
        balance, allowance, balance_refreshed, allowance_refreshed = balance_result

        # data API 返回的 redeemable=True 持仓：cur_price/current_value 被
        # to_position 故意 drop（避免 stale curPrice），到这里都是 None。在写入
        # account_state 之前用 gamma 派定胜负 → cur_price=1/0 → settled_zero_value
        # 即刻为 True/False，UI 不必等下一轮 5-min settlement_scanner 才看到正确语义。
        if positions is not None and self._gamma_client is not None:
            positions = await self._enrich_redeemable_positions(positions, failures)

        if self._account_state_store is not None:
            if positions is not None:
                self._account_state_store.replace_positions(positions)
            if open_orders is not None:
                self._account_state_store.replace_open_orders(open_orders)
            if positions is not None or open_orders is not None:
                self._refresh_authoritative_position_coverage()
            if fills is not None:
                self._account_state_store.replace_fills(fills)
            if balance_refreshed or allowance_refreshed:
                self._account_state_store.update_balances(
                    balance_usdc=balance,
                    allowance_usdc=allowance,
                )

        return AuthoritativeRefreshSummary(
            trace_id=trace_id,
            market_count=len(markets),
            refreshed_markets=0,
            refreshed_orderbooks=0,
            refreshed_fee_rates=0,
            refreshed_positions=0 if positions is None else len(positions),
            refreshed_open_orders=0 if open_orders is None else len(open_orders),
            refreshed_fills=0 if fills is None else len(fills),
            refreshed_balance=balance_refreshed,
            refreshed_allowance=allowance_refreshed,
            user_refresh_enabled=True,
            failures=tuple(failures),
        )

    def _refresh_authoritative_position_coverage(self) -> None:
        """用权威持仓和权威开放订单重建持仓覆盖字段。"""

        if self._account_state_store is None:
            return
        snapshot = self._account_state_store.snapshot()
        projector = AccountStateProjector(
            self._account_state_store,
        )
        coverage_targets: dict[tuple[str, str], str | None] = {}
        for position in snapshot.positions:
            coverage_targets[(position.condition_id, position.token_id)] = position.market_slug
        for order in snapshot.open_orders:
            coverage_targets.setdefault((order.condition_id, order.token_id), order.market_slug)

        for (condition_id, token_id), market_slug in coverage_targets.items():
            current_snapshot = self._account_state_store.snapshot()
            current_position = current_snapshot.get_position(condition_id, token_id)
            resolved_market_slug = (
                current_position.market_slug
                if current_position is not None and current_position.market_slug
                else market_slug
            )
            # Data API 的持仓不携带本地挂单覆盖量；权威刷新必须把 CLOB 开放订单重新投影回 Position，
            # 否则风控和前端会误判仍有未覆盖持仓并重复提交退出单。
            projector.refresh_position_coverage(
                condition_id=condition_id,
                token_id=token_id,
                market_slug=resolved_market_slug,
                last_order_id=None if current_position is None else current_position.last_order_id,
                last_trade_id=None if current_position is None else current_position.last_trade_id,
                confirmation_status=(
                    "authority_refresh"
                    if current_position is None
                    else current_position.confirmation_status
                ),
                updated_at=None if current_position is None else current_position.updated_at,
            )

    def _target_markets(self, *, condition_ids: tuple[str, ...] | None = None) -> tuple[Market, ...]:
        condition_id_filter = set(condition_ids or ())
        if self._registry_snapshot_provider is not None:
            markets = self._registry_snapshot_provider().markets
        elif self._registry is not None:
            markets = self._registry.snapshot().markets
        else:
            markets = tuple()
        if not condition_id_filter:
            return markets
        return tuple(market for market in markets if market.condition_id in condition_id_filter)

    def _market_authority_targets(
        self,
        markets: tuple[Market, ...],
        *,
        condition_ids: tuple[str, ...] | None,
        refresh_market_authority: bool,
    ) -> tuple[Market, ...]:
        if not refresh_market_authority:
            return ()
        if condition_ids is not None:
            return markets
        if self._account_state_store is None:
            return ()
        account_snapshot = self._account_state_store.snapshot()
        return tuple(market for market in markets if _market_has_account_exposure(account_snapshot, market))

    def _missing_account_exposure_targets(
        self,
        *,
        existing_markets: tuple[Market, ...],
        condition_ids: tuple[str, ...] | None,
    ) -> tuple[Market, ...]:
        """从账户持仓反向补齐 registry 缺失的 market。

        全量扫描和 registry 是市场发现视角；账户持仓是风险管理视角。
        只要账户里仍有敞口，reconcile 就必须能恢复 market 快照并生成退出计划。
        """

        if self._account_state_store is None:
            return ()
        account_snapshot = self._account_state_store.snapshot()
        condition_id_filter = set(condition_ids or ())
        known_condition_ids = {market.condition_id for market in existing_markets}
        if self._registry is not None:
            known_condition_ids.update(market.condition_id for market in self._registry.snapshot().markets)

        targets: dict[str, Market] = {}
        for condition_id, token_id, market_slug in _account_exposure_market_refs(account_snapshot):
            if condition_id in known_condition_ids or condition_id in targets:
                continue
            if condition_id_filter and condition_id not in condition_id_filter:
                continue
            synthetic_slug = market_slug or f"account-exposure-{condition_id}"
            targets[condition_id] = Market(
                condition_id=condition_id,
                market_slug=synthetic_slug,
                event_slug=synthetic_slug,
                outcomes=(MarketOutcome(token_id=token_id, outcome=""),),
                trading_status=TradingStatus.CANDIDATE,
            )
        return tuple(targets.values())

    async def _refresh_market_authority_targets(
        self,
        markets: tuple[Market, ...],
    ) -> tuple[AuthoritativeMarketRefresh | BaseException, ...]:
        if not markets:
            return ()
        # 死链回收 sweep：先把已 quarantine 且无敞口的 market 从 registry 删掉，
        # 避免它们永久挂 PAUSED。两类来源都覆盖：
        #   1. 本进程内 failure_count >= threshold（活跃 quarantine）
        #   2. 跨进程重启后 failure_count 归零、但 market 仍带 auto_quarantine_dead_market
        #      reject_reason（registry 持久化态）
        # 有敞口（持仓 shares>0 或 open order）的不动，仍走 PAUSED 等结算/redeem。
        if self._registry is not None:
            for m in markets:
                quarantined = (
                    self._condition_failure_counts.get(m.condition_id, 0) >= self._quarantine_threshold
                    or m.reject_reason == "auto_quarantine_dead_market"
                )
                if quarantined and self._has_no_chain_exposure(m.condition_id):
                    self._registry.remove_market(m.condition_id)
        # 自动 quarantine 过滤：连续失败超阈值的 condition 直接跳过 refresh,
        # 让 reconcile failures 不再被这些死 market 撑爆触发 degraded。
        active_markets = tuple(
            m for m in markets
            if self._condition_failure_counts.get(m.condition_id, 0) < self._quarantine_threshold
        )
        if not active_markets:
            return ()
        semaphore = asyncio.Semaphore(self._market_authority_concurrency)

        async def _refresh_one(market: Market) -> AuthoritativeMarketRefresh:
            async with semaphore:
                return await self._refresh_market_authority(market)

        return await asyncio.gather(*(_refresh_one(market) for market in active_markets), return_exceptions=True)

    async def _refresh_market_authority(self, market: Market) -> AuthoritativeMarketRefresh:
        failures: list[AuthoritativeRefreshFailure] = []
        refreshed_market = await self._fetch_gamma_market(market, failures)
        market_for_orderbook = refreshed_market or market
        fee_rate_bps = None
        if (
            market_for_orderbook.fee_rate_bps is None
            and market_for_orderbook.taker_base_fee_bps is None
        ):
            fee_rate_bps = await self._fetch_fee_rate(market_for_orderbook, failures)
        fee_rate_refreshed = fee_rate_bps is not None
        if fee_rate_bps is not None:
            market_for_orderbook = market_for_orderbook.with_fee_rate(
                fee_rate_bps,
                fee_rate_updated_at=_utc_now(),
            )
            refreshed_market = market_for_orderbook
        # 不再拉 /book REST——盘口由 market_ws 实时 push 直接维护 in-memory
        # state，reconcile 不复制这条数据。WS 是唯一真相源，sequence_gap 兜底
        # 由 market_ws_worker 内部自管（worker.py:_handle_sequence_gap）。
        # quarantine 活体信号：gamma 拿到 market + WS 有新鲜快照，任一成功即不算
        # dead。两者都缺失才计入失败计数。
        ws_alive = self._market_ws_has_fresh_snapshot(market_for_orderbook.token_ids)
        is_dead = refreshed_market is None and not ws_alive
        if is_dead:
            self._condition_failure_counts[market.condition_id] = (
                self._condition_failure_counts.get(market.condition_id, 0) + 1
            )
            if self._condition_failure_counts[market.condition_id] >= self._quarantine_threshold:
                self._apply_quarantine_pause(market)
        else:
            self._condition_failure_counts.pop(market.condition_id, None)
        return AuthoritativeMarketRefresh(
            requested_market=market,
            refreshed_market=refreshed_market,
            fee_rate_refreshed=fee_rate_refreshed,
            failures=tuple(failures),
        )

    def _apply_quarantine_pause(self, market: Market) -> None:
        """连续失败超阈值 → 死链回收或标 PAUSED。

        优先路径：无任何链上敞口（持仓 shares=0 且无 open order）→ 直接从
        registry remove_market，不再永久挂 PAUSED 簿记。这是 §7 WS-driven
        prune 的兜底——`auto_quarantine_dead_market` 只是标黑而不回收会让
        registry 堆满 2 天前的死链，被 SSE 面板误读成"有头寸"。

        保留 §15 红线：只要还有 shares > 0 或 open order，仍然只标 PAUSED，
        因为可能在等结算/redeem，不能 prune。
        """
        if self._registry is None:
            return
        if self._has_no_chain_exposure(market.condition_id):
            self._registry.remove_market(market.condition_id)
            return
        paused = market.with_trading_status(
            TradingStatus.PAUSED,
            reject_reason="auto_quarantine_dead_market",
        )
        self._registry.upsert(paused)

    def _has_no_chain_exposure(self, condition_id: str) -> bool:
        """判断 condition 是否完全无敞口可以安全 prune。

        无 AccountStateStore 时保守返回 False（保留 PAUSED 簿记，留给后续
        reconcile 周期再判断），避免错杀仍在 redeem 的持仓。
        """
        if self._account_state_store is None:
            return False
        snapshot = self._account_state_store.snapshot()
        for position in snapshot.positions:
            if position.condition_id == condition_id and position.shares > Decimal("0"):
                return False
        for order in snapshot.open_orders:
            if order.condition_id == condition_id:
                return False
        return True

    async def _fetch_gamma_market(
        self,
        market: Market,
        failures: list[AuthoritativeRefreshFailure],
    ) -> Market | None:
        if self._gamma_client is None:
            return None
        # 优先读 gamma_snapshot_store——discovery 每 0.5s 已经把 events.markets
        # 写进去，30s 内的 entry 一定新鲜，跳过网络调用直接返回 merged Market。
        if self._gamma_snapshot_store is not None:
            cached = self._gamma_snapshot_store.get_dto_if_fresh(
                market.condition_id, max_age_s=_GAMMA_SNAPSHOT_FRESH_WINDOW_S
            )
            if cached is not None:
                clob_enabled = getattr(cached, "clob_enabled", True)
                return self._merge_gamma_market(
                    market,
                    cached.to_market(),
                    bool(clob_enabled) if clob_enabled is not None else True,
                )
        for slug in (market.market_slug, market.event_slug):
            if not slug:
                continue
            candidates = await _await_authority(
                component="gamma",
                operation="list_markets",
                failures=failures,
                awaitable=self._gamma_client.list_markets(
                    slug=str(slug),
                    active=None,
                    closed=None,
                    limit=25,
                ),
                target=f"{market.condition_id}:{slug}",
                timeout_s=self._authority_call_timeout_s,
            )
            if candidates is None:
                continue
            gamma_market = _pick_gamma_market(candidates, market)
            if gamma_market is None:
                failures.append(
                    AuthoritativeRefreshFailure(
                        component="gamma",
                        operation="list_markets",
                        target=f"{market.condition_id}:{slug}",
                        reason="not_found",
                        retryable=True,
                    )
                )
                continue
            refreshed_market = self._merge_gamma_market(
                market,
                gamma_market.to_market(),
                gamma_market.clob_enabled,
            )
            return refreshed_market
        return None

    def _market_ws_has_fresh_snapshot(self, token_ids: tuple[str, ...]) -> bool:
        """市场至少有一个 token 拥有新鲜 WS 快照——liveness 信号。

        用于 quarantine 判定：gamma 拿不到时，只要 WS 仍在推快照说明市场活着；
        gamma + WS 都缺才计入失败计数。
        """
        if self._market_ws_worker is None or not token_ids:
            return False
        now = _utc_now()
        for token_id in token_ids:
            snap = self._market_ws_worker.snapshot(token_id)
            if snap is None or snap.received_at is None:
                continue
            if (now - snap.received_at).total_seconds() <= _WS_ORDERBOOK_FRESH_WINDOW_S:
                return True
        return False

    def _apply_refreshed_market(self, market: Market) -> None:
        if self._market_ws_worker is not None:
            self._market_ws_worker.track_market(market)
            return
        if self._registry is not None:
            self._registry.upsert(market)

    def _merge_gamma_market(
        self,
        current: Market,
        refreshed: Market,
        clob_enabled: bool | None,
    ) -> Market:
        merged = current.with_metadata(
            event_id=refreshed.event_id,
            event_title=refreshed.event_title,
            event_slug=refreshed.event_slug,
            icon_url=refreshed.icon_url,
            end_date=refreshed.end_date,
            game_start_time=refreshed.game_start_time,
            category=refreshed.category,
            tags=refreshed.tags,
            matched_keywords=current.matched_keywords,
            outcomes=refreshed.outcomes,
            neg_risk=refreshed.neg_risk,
        )
        merged = merged.with_tick_size(refreshed.tick_size)
        merged = merged.with_min_order_size(refreshed.min_order_size)
        merged = merged.with_fee_schedule(
            fees_enabled=refreshed.fees_enabled,
            maker_base_fee_bps=refreshed.maker_base_fee_bps,
            taker_base_fee_bps=refreshed.taker_base_fee_bps,
        )

        desired_status = refreshed.trading_status
        reject_reason = refreshed.reject_reason
        if clob_enabled is False and desired_status == TradingStatus.ELIGIBLE:
            desired_status = TradingStatus.PAUSED
            reject_reason = reject_reason or "orderbook_disabled"

        if current.trading_status in {
            TradingStatus.RESOLVED,
            TradingStatus.REJECTED,
        }:
            desired_status = current.trading_status
            reject_reason = current.reject_reason
        elif (
            current.trading_status == TradingStatus.PAUSED
            and current.reject_reason == "manual_pause"
            and desired_status == TradingStatus.ELIGIBLE
        ):
            desired_status = TradingStatus.PAUSED
            reject_reason = current.reject_reason

        return merged.with_trading_status(desired_status, reject_reason=reject_reason)

    async def _fetch_fee_rate(
        self,
        market: Market,
        failures: list[AuthoritativeRefreshFailure],
    ) -> int | None:
        if self._clob_client is None:
            return None
        token_id = next(iter(market.token_ids), None)
        if token_id is None:
            return None
        return await _await_authority(
            component="clob",
            operation="fee_rate",
            failures=failures,
            awaitable=self._clob_client.get_fee_rate(token_id),
            target=f"{market.condition_id}:{token_id}",
            timeout_s=self._authority_call_timeout_s,
        )

    async def _fetch_positions(
        self,
        failures: list[AuthoritativeRefreshFailure],
    ) -> tuple[Position, ...] | None:
        if self._data_client is None:
            return None
        positions = await _await_authority(
            component="data",
            operation="positions",
            failures=failures,
            awaitable=self._data_client.list_positions(),
            timeout_s=self._authority_call_timeout_s,
        )
        if positions is None:
            return None
        return tuple(position.to_position() for position in positions)

    async def _enrich_redeemable_positions(
        self,
        positions: tuple[Position, ...],
        failures: list[AuthoritativeRefreshFailure],
    ) -> tuple[Position, ...]:
        """对 redeemable=True 且 cur_price=None 的持仓并发拉 gamma outcomePrices，
        派定胜负后写 cur_price/current_value/cash_pnl/percent_pnl。

        其它持仓直接透传。胜负 unknown（gamma 没返 outcomePrices 或失败）也透传
        ——保守不动等下轮 settlement_scanner 周期兜底。
        """
        from polymarket_trader.app.settlement_scanner import (
            _resolve_from_gamma_payload,
            apply_outcome_to_position,
        )

        targets_idx = [
            i
            for i, p in enumerate(positions)
            if p.redeemable is True and p.cur_price is None
        ]
        if not targets_idx:
            return positions

        # 同 condition_id 只查一次 gamma。
        unique_cids: list[str] = []
        seen_cids: set[str] = set()
        for i in targets_idx:
            cid = positions[i].condition_id
            if cid and cid not in seen_cids:
                seen_cids.add(cid)
                unique_cids.append(cid)

        async def _resolve(cid: str) -> tuple[str, str | None]:
            candidate = await _await_authority(
                component="gamma",
                operation="resolve_by_condition",
                failures=failures,
                awaitable=self._gamma_client.get_market_by_condition_id(
                    cid, timeout_s=self._authority_call_timeout_s
                ),
                target=cid,
                timeout_s=self._authority_call_timeout_s,
            )
            if candidate is None:
                return cid, None
            resolved = _resolve_from_gamma_payload(cid, candidate)
            if resolved is None or not resolved.closed:
                return cid, None
            return cid, resolved.winning_token_id

        results = await asyncio.gather(*(_resolve(cid) for cid in unique_cids))
        winner_by_cid: dict[str, str | None] = dict(results)

        enriched_list = list(positions)
        for i in targets_idx:
            pos = enriched_list[i]
            winning_token_id = winner_by_cid.get(pos.condition_id)
            if winning_token_id is None:
                continue
            updated = apply_outcome_to_position(pos, winning_token_id=winning_token_id)
            if updated is not None:
                enriched_list[i] = updated
        return tuple(enriched_list)

    async def _fetch_open_orders(
        self,
        failures: list[AuthoritativeRefreshFailure],
    ) -> tuple[OrderRecord, ...] | None:
        if self._clob_client is None:
            return None
        orders = await _await_authority(
            component="clob",
            operation="open_orders",
            failures=failures,
            awaitable=self._clob_client.list_open_orders(),
            timeout_s=self._authority_call_timeout_s,
        )
        if orders is None:
            return None
        return tuple(order.to_order_record() for order in orders)

    async def _fetch_fills(
        self,
        failures: list[AuthoritativeRefreshFailure],
    ) -> tuple[Fill, ...] | None:
        if self._clob_client is None:
            return None
        fills = await _await_authority(
            component="clob",
            operation="fills",
            failures=failures,
            awaitable=self._clob_client.list_fills(),
            timeout_s=self._authority_call_timeout_s,
        )
        if fills is None:
            return None
        return tuple(fill.to_fill() for fill in fills)

    async def _fetch_balance(
        self,
        failures: list[AuthoritativeRefreshFailure],
    ) -> tuple[Decimal, Decimal, bool, bool]:
        if self._clob_client is None:
            return Decimal("0"), Decimal("0"), False, False
        balance = await _await_authority(
            component="clob",
            operation="balance_allowance",
            failures=failures,
            awaitable=self._clob_client.get_balance_allowance(),
            timeout_s=self._authority_call_timeout_s,
        )
        if balance is None:
            return Decimal("0"), Decimal("0"), False, False
        return balance.balance_usdc, balance.allowance_usdc, True, True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _market_has_account_exposure(account_snapshot: Any, market: Market) -> bool:
    for token_id in market.token_ids:
        position = account_snapshot.get_position(market.condition_id, token_id)
        if position is not None and (
            not position.settled_zero_value
            and (
                position.shares > 0
                or position.open_buy_shares > 0
                or position.open_sell_shares > 0
                or position.pending_buy_shares > 0
            )
        ):
            return True
        if account_snapshot.open_orders_for_market(market.condition_id, token_id):
            return True
    return False


def _account_exposure_market_refs(account_snapshot: Any) -> tuple[tuple[str, str, str | None], ...]:
    # 只从 active positions(非 settled_zero、有 shares/open_orders)+ open_orders
    # derive market refs。fills 是历史成交审计存档,不代表当前账户暴露——
    # 用 fills 推会让 reconcile 反复查 N 条历史 fill 涉及的死孤儿市场
    # (实测 38 条 fills 让 startup reconcile 多查 111 次 not_found,占启动 ~10s)。
    # 真有 active exposure 的 condition_id 必然出现在 positions/open_orders 里。
    refs: dict[tuple[str, str], str | None] = {}
    for position in account_snapshot.positions:
        if position.settled_zero_value:
            continue
        if (
            position.shares <= 0
            and position.open_buy_shares <= 0
            and position.open_sell_shares <= 0
            and position.pending_buy_shares <= 0
        ):
            continue
        refs[(position.condition_id, position.token_id)] = position.market_slug
    for order in account_snapshot.open_orders:
        refs[(order.condition_id, order.token_id)] = order.market_slug or refs.get(
            (order.condition_id, order.token_id)
        )
    return tuple((condition_id, token_id, market_slug) for (condition_id, token_id), market_slug in refs.items())


async def _await_authority(
    *,
    component: str,
    operation: str,
    failures: list[AuthoritativeRefreshFailure],
    awaitable: Awaitable[_T],
    target: str | None = None,
    timeout_s: float = _AUTHORITY_CALL_TIMEOUT_S,
) -> _T | None:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except TimeoutError:
        failures.append(
            AuthoritativeRefreshFailure(
                component=component,
                operation=operation,
                target=target,
                reason="timeout",
            )
        )
    except Exception as exc:
        failures.append(
            AuthoritativeRefreshFailure(
                component=component,
                operation=operation,
                target=target,
                reason="exception",
                detail=str(exc),
            )
        )
    return None


def _pick_gamma_market(
    candidates: tuple[GammaMarketCandidate, ...],
    market: Market,
) -> GammaMarketCandidate | None:
    for candidate in candidates:
        if candidate.condition_id == market.condition_id:
            return candidate
    for candidate in candidates:
        candidate_token_ids = {outcome.token_id for outcome in candidate.outcomes}
        if candidate_token_ids.intersection(market.token_ids):
            return candidate
    for candidate in candidates:
        if candidate.market_slug == market.market_slug:
            return candidate
    return None
