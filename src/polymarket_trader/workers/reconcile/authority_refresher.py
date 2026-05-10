from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol, TypeVar, cast

from polymarket_trader.app.order_projection import AccountStateProjector
from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import OrderRecord
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.registry import MarketRegistry, MarketRegistrySnapshot
from polymarket_trader.workers.market_ws import MarketWsWorker

RegistrySnapshotProvider = Callable[[], MarketRegistrySnapshot]
_AUTHORITY_CALL_TIMEOUT_S = 5.0
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
    ) -> tuple[Any, ...]: ...


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
    condition_id: str
    market_slug: str
    clob_enabled: bool | None
    outcomes: tuple[Any, ...]

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
    orderbook_snapshots: tuple[OrderbookSnapshot, ...] = ()
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
    ) -> None:
        self._registry_snapshot_provider = registry_snapshot_provider
        self._account_state_store = account_state_store
        self._registry = registry
        self._market_ws_worker = market_ws_worker
        self._gamma_client = gamma_client
        self._clob_client = clob_client
        self._data_client = data_client
        self._trading_client = trading_client
        self._authority_call_timeout_s = (
            _AUTHORITY_CALL_TIMEOUT_S
            if authority_call_timeout_s is None
            else authority_call_timeout_s
        )
        self._market_authority_concurrency = max(1, market_authority_concurrency)

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
            if item.orderbook_snapshots:
                refreshed_orderbooks += len(item.orderbook_snapshots)
                await self._apply_refreshed_orderbooks(
                    item.refreshed_market or item.requested_market,
                    item.orderbook_snapshots,
                )
            if item.fee_rate_refreshed:
                refreshed_fee_rates += 1
            refresh_failures.extend(item.failures)

        account_summary = await self.refresh_account(trace_id=trace_id, markets=markets)
        refresh_failures.extend(account_summary.failures)

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
            if item.orderbook_snapshots:
                refreshed_orderbooks += len(item.orderbook_snapshots)
                await self._apply_refreshed_orderbooks(
                    item.refreshed_market or item.requested_market,
                    item.orderbook_snapshots,
                )
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
        if not user_refresh_enabled:
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
        projector = AccountStateProjector(self._account_state_store)
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
        semaphore = asyncio.Semaphore(self._market_authority_concurrency)

        async def _refresh_one(market: Market) -> AuthoritativeMarketRefresh:
            async with semaphore:
                return await self._refresh_market_authority(market)

        return await asyncio.gather(*(_refresh_one(market) for market in markets), return_exceptions=True)

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
        orderbook_snapshots = await self._fetch_orderbook_snapshots(
            market_for_orderbook,
            failures,
        )
        return AuthoritativeMarketRefresh(
            requested_market=market,
            refreshed_market=refreshed_market,
            orderbook_snapshots=orderbook_snapshots,
            fee_rate_refreshed=fee_rate_refreshed,
            failures=tuple(failures),
        )

    async def _fetch_gamma_market(
        self,
        market: Market,
        failures: list[AuthoritativeRefreshFailure],
    ) -> Market | None:
        if self._gamma_client is None:
            return None
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

    async def _fetch_orderbook_snapshots(
        self,
        market: Market,
        failures: list[AuthoritativeRefreshFailure],
    ) -> tuple[OrderbookSnapshot, ...]:
        if self._clob_client is None or not hasattr(self._clob_client, "get_orderbook"):
            return ()
        snapshots: list[OrderbookSnapshot] = []
        for token_id in market.token_ids:
            orderbook = await _await_authority(
                component="clob",
                operation="orderbook",
                failures=failures,
                awaitable=self._clob_client.get_orderbook(
                    token_id,
                    market_slug=market.market_slug,
                    condition_id=market.condition_id,
                ),
                target=f"{market.condition_id}:{token_id}",
                timeout_s=self._authority_call_timeout_s,
            )
            if orderbook is None:
                continue
            snapshots.append(orderbook.to_snapshot())
        return tuple(snapshots)

    async def _apply_refreshed_orderbooks(
        self,
        market: Market,
        snapshots: tuple[OrderbookSnapshot, ...],
    ) -> None:
        if self._market_ws_worker is None:
            return
        for snapshot in snapshots:
            await self._market_ws_worker.apply_rest_snapshot(
                snapshot.token_id,
                snapshot,
                source="reconcile_rest",
            )

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
        if self._clob_client is None or not hasattr(self._clob_client, "get_fee_rate"):
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
    for fill in account_snapshot.fills:
        if fill.condition_id is None or fill.token_id is None:
            continue
        refs[(fill.condition_id, fill.token_id)] = fill.market_slug or refs.get(
            (fill.condition_id, fill.token_id)
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
    except Exception as exc:  # pragma: no cover - external SDK failure path
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
    candidates: tuple[object, ...],
    market: Market,
) -> GammaMarketCandidate | None:
    for candidate in candidates:
        if getattr(candidate, "condition_id", None) == market.condition_id:
            return cast(GammaMarketCandidate, candidate)
    for candidate in candidates:
        candidate_outcomes = getattr(candidate, "outcomes", ())
        candidate_token_ids = tuple(
            getattr(outcome, "token_id", None)
            for outcome in candidate_outcomes
            if getattr(outcome, "token_id", None)
        )
        if set(candidate_token_ids).intersection(market.token_ids):
            return cast(GammaMarketCandidate, candidate)
    for candidate in candidates:
        if getattr(candidate, "market_slug", None) == market.market_slug:
            return cast(GammaMarketCandidate, candidate)
    return None
