"""MarketMiscAggregator —— 市场盘口 / 流动性 / 数据健康 / 历史快照 / 派生指标。

market_detail / settlement / portfolio exposure 在各自专门 aggregator；
本 aggregator 收 market 维度其余查询（约 16 个方法）。按 docs/新架构方案.md
§12.2 三类划分：
- list_markets / list_orderbook_history → DB 审计查询
- get_market_orderbook / midpoint / prices_history → REST + WS 混合
- orderbook_direction / depth / liquidity_summary / event_bundle /
  market_data_health / get_market_liquidity / get_market_impact → 内存运营查询

# Endpoint 对应

| Endpoint | 方法 |
|---|---|
| `GET /markets` | `list_markets(...)` |
| `GET /markets/orderbook` | `get_market_orderbook(...)` |
| `GET /markets/midpoint` | `get_market_midpoint(...)` |
| `GET /markets/orderbook-direction` | `get_orderbook_direction(...)` / `get_orderbook_direction_multi(...)` |
| `GET /markets/orderbook-depth` | `orderbook_depth_snapshot(...)` |
| `GET /markets/liquidity-summary` | `liquidity_summary_snapshot(...)` |
| `GET /markets/event-bundle` | `event_bundle_snapshot(...)` |
| `GET /markets/data-health` | `market_data_health_snapshot(...)` |
| `GET /markets/orderbook-history` | `list_orderbook_history(...)` |
| `GET /markets/prices-history` | `get_market_prices_history(...)` |
| `GET /markets/liquidity` | `get_market_liquidity(...)` |
| `GET /markets/impact` | `get_market_impact(...)` |
| `GET /runtime/trade-tape` | `market_trade_tape(...)` |
| `GET /runtime/derived-metrics` | `derived_metrics_snapshot(...)` |
| `GET /runtime/odds-drift` | `odds_drift_snapshot(...)` |
| `GET /runtime/arbitrage` | `arbitrage_snapshot()` |
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from typing import Literal

from polymarket_trader.api.serialization import AdminSerializer
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.infra.db.repositories.market import sort_markets
from polymarket_trader.infra.polymarket import PolymarketClientError
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.serialization import decimal_text, page_payload


MarketFeeSortField = Literal[
    "market_slug",
    "fee_rate_bps",
    "fee_rate_updated_at",
    "maker_base_fee_bps",
    "taker_base_fee_bps",
]
SortDirection = Literal["asc", "desc"]


def _orderbook_has_no_quotes(snapshot: OrderbookSnapshot) -> bool:
    """判断热态盘口是否只是空占位（market_ws 先建空快照，查询侧不能误认有效）。"""
    return (
        snapshot.best_bid is None
        and snapshot.best_ask is None
        and not snapshot.bids
        and not snapshot.asks
    )


def _market_matches_fee_filters(
    market: Market,
    *,
    fees_enabled: bool | None = None,
    fee_rate_bps_min: int | None = None,
    fee_rate_bps_max: int | None = None,
    maker_base_fee_bps_min: int | None = None,
    maker_base_fee_bps_max: int | None = None,
    taker_base_fee_bps_min: int | None = None,
    taker_base_fee_bps_max: int | None = None,
) -> bool:
    if fees_enabled is not None and market.fees_enabled is not fees_enabled:
        return False
    if fee_rate_bps_min is not None and (
        market.fee_rate_bps is None or market.fee_rate_bps < fee_rate_bps_min
    ):
        return False
    if fee_rate_bps_max is not None and (
        market.fee_rate_bps is None or market.fee_rate_bps > fee_rate_bps_max
    ):
        return False
    if maker_base_fee_bps_min is not None and (
        market.maker_base_fee_bps is None
        or market.maker_base_fee_bps < maker_base_fee_bps_min
    ):
        return False
    if maker_base_fee_bps_max is not None and (
        market.maker_base_fee_bps is None
        or market.maker_base_fee_bps > maker_base_fee_bps_max
    ):
        return False
    if taker_base_fee_bps_min is not None and (
        market.taker_base_fee_bps is None
        or market.taker_base_fee_bps < taker_base_fee_bps_min
    ):
        return False
    if taker_base_fee_bps_max is not None and (
        market.taker_base_fee_bps is None
        or market.taker_base_fee_bps > taker_base_fee_bps_max
    ):
        return False
    return True

from ._db import RepositoryGroup, with_repositories
from ._helpers import slice_sequence

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _serialize_side_summary(side: Any) -> dict[str, Any]:
    def _lvl(lv: Any) -> dict[str, str]:
        return {"price": str(lv.price), "size": str(lv.size), "usdc": str(lv.usdc)}
    return {
        "total_size": str(side.total_size),
        "total_usdc": str(side.total_usdc),
        "level_count": side.level_count,
        "levels_top20": [_lvl(lv) for lv in side.levels_top20],
        "median_price": str(side.median_price) if side.median_price is not None else None,
        "median_size_cumulative": str(side.median_size_cumulative),
        "edge_price": str(side.edge_price),
        "edge_size": str(side.edge_size),
        "edge_usdc": str(side.edge_usdc),
        "whale_threshold_usdc": str(side.whale_threshold_usdc),
        "whale_count": side.whale_count,
        "whale_total_usdc": str(side.whale_total_usdc),
        "whale_total_size": str(side.whale_total_size),
        "whale_levels": [_lvl(lv) for lv in side.whale_levels],
    }


def _serialize_window_delta(w: Any) -> dict[str, Any]:
    base = {"window_s": w.window_s, "available": w.available}
    if not w.available:
        return base
    base.update({
        "bid_total_size_delta": str(w.bid_total_size_delta),
        "ask_total_size_delta": str(w.ask_total_size_delta),
        "bid_total_usdc_delta": str(w.bid_total_usdc_delta),
        "ask_total_usdc_delta": str(w.ask_total_usdc_delta),
        "best_bid_delta": str(w.best_bid_delta) if w.best_bid_delta is not None else None,
        "best_ask_delta": str(w.best_ask_delta) if w.best_ask_delta is not None else None,
        "mid_delta": str(w.mid_delta) if w.mid_delta is not None else None,
        "bid_whale_count_delta": w.bid_whale_count_delta,
        "ask_whale_count_delta": w.ask_whale_count_delta,
        "bid_whale_usdc_delta": str(w.bid_whale_usdc_delta),
        "ask_whale_usdc_delta": str(w.ask_whale_usdc_delta),
    })
    return base


def _serialize_liquidity_metrics(derived: Any) -> dict[str, Any]:
    def _s(v: Any) -> str | None:
        return str(v) if v is not None else None
    return {
        "mid": _s(derived.mid),
        "spread_abs": _s(derived.spread_abs),
        "relative_spread_bps": _s(derived.relative_spread_bps),
        "implied_prob_lower": _s(derived.implied_prob_lower),
        "implied_prob_upper": _s(derived.implied_prob_upper),
        "implied_prob_mid": _s(derived.implied_prob_mid),
        "microprice": _s(derived.microprice),
        "microprice_divergence_bps": _s(derived.microprice_divergence_bps),
        "quoted_depth_at_best": {
            "bid_size": str(derived.quoted_depth_at_best_bid_size),
            "ask_size": str(derived.quoted_depth_at_best_ask_size),
            "bid_usdc": str(derived.quoted_depth_at_best_bid_usdc),
            "ask_usdc": str(derived.quoted_depth_at_best_ask_usdc),
            "total_size": str(derived.quoted_depth_at_best_total_size),
        },
        "depth_imbalance": _s(derived.depth_imbalance),
        "price_impact": [
            {
                "target_usdc": str(p.target_usdc),
                "filled": p.filled,
                **({"fillable_usdc": str(p.fillable_usdc)} if not p.filled else {}),
                **({
                    "avg_price": _s(p.avg_price),
                    "worst_price": _s(p.worst_price),
                    "slippage_bps_vs_best_ask": _s(p.slippage_bps_vs_best_ask),
                } if p.filled else {}),
            }
            for p in derived.price_impact
        ],
        "fillable_within_slippage": [
            {
                "slippage_bps_max": str(f.slippage_bps_max),
                "max_price": _s(f.max_price),
                "fillable_usdc": str(f.fillable_usdc),
            }
            for f in derived.fillable_within_slippage
        ],
        "liquidity_score": str(derived.liquidity_score),
        "liquidity_tier": derived.liquidity_tier,
        "sample_count_15s": derived.sample_count_15s,
        "quote_update_rate_per_s": _s(derived.quote_update_rate_per_s),
        "mid_volatility_15s": _s(derived.mid_volatility_15s),
        "mid_cv_15s_bps": _s(derived.mid_cv_15s_bps),
        "mid_direction_reversals_15s": derived.mid_direction_reversals_15s,
        "mid_change_total_15s": _s(derived.mid_change_total_15s),
        "spread_volatility_15s": _s(derived.spread_volatility_15s),
        "spread_max_15s": _s(derived.spread_max_15s),
        "spread_min_15s": _s(derived.spread_min_15s),
        "spread_avg_15s": _s(derived.spread_avg_15s),
        "resilience_score": str(derived.resilience_score),
        "direction": (
            {
                "window_seconds": derived.direction.window_seconds,
                "sample_count": derived.direction.sample_count,
                "direction_score": str(derived.direction.direction_score),
                "price_momentum": str(derived.direction.price_momentum),
                "flow_imbalance": str(derived.direction.flow_imbalance),
                "direction_label": derived.direction.direction_label,
                "confidence": str(derived.direction.confidence),
            }
            if derived.direction is not None
            else None
        ),
    }


def _classify_market(market: Any) -> dict[str, Any]:
    if market is None:
        return {"available": False, "reason": "market_not_found_in_registry"}
    smt = getattr(market, "sports_market_type", None)
    slug = (getattr(market, "market_slug", "") or "").lower()
    question = getattr(market, "market_question", None)
    inferred = None
    if not smt:
        if "first-blood" in slug or "first-touchdown" in slug or "first-goal" in slug or "first-set" in slug:
            inferred = "first_event_prop"
        elif "spread-" in slug or "handicap" in slug:
            inferred = "spreads_candidate"
        elif "over-" in slug or "under-" in slug or "-totals" in slug or "points-scored" in slug or "total-goals" in slug:
            inferred = "totals_candidate"
        elif (
            "1h-" in slug or "2h-" in slug or "first-half" in slug or "second-half" in slug
            or "q1-" in slug or "q2-" in slug or "q3-" in slug or "q4-" in slug
            or "first-inning" in slug or "first-set" in slug or "second-set" in slug
        ):
            inferred = "subperiod_candidate"
        elif "champion" in slug or "winner" in slug or "to-win-" in slug:
            inferred = "futures_candidate"
        elif "-vs-" in slug or "moneyline" in slug:
            inferred = "moneyline_candidate"
    return {
        "available": True,
        "sports_market_type": smt,
        "inferred_kind": inferred,
        "market_question": question,
        "event_slug": getattr(market, "event_slug", None),
        "market_slug": getattr(market, "market_slug", None),
        "trading_status": (
            getattr(market.trading_status, "value", None)
            if getattr(market, "trading_status", None) is not None else None
        ),
    }


def _serialize_direction_signal(signal: Any) -> dict[str, Any]:
    def _opt(d: Any) -> str | None:
        return None if d is None else str(d)
    return {
        "window_seconds": signal.window_seconds,
        "sample_count": signal.sample_count,
        "first_observed_at": signal.first_observed_at.isoformat(),
        "last_observed_at": signal.last_observed_at.isoformat(),
        "bid_price_delta": _opt(signal.bid_price_delta),
        "ask_price_delta": _opt(signal.ask_price_delta),
        "mid_price_delta": _opt(signal.mid_price_delta),
        "bid_size_delta": _opt(signal.bid_size_delta),
        "ask_size_delta": _opt(signal.ask_size_delta),
        "first_microprice": _opt(signal.first_microprice),
        "last_microprice": _opt(signal.last_microprice),
        "microprice_delta": _opt(signal.microprice_delta),
        "first_real_bid_depth_usdc": _opt(signal.first_real_bid_depth_usdc),
        "last_real_bid_depth_usdc": _opt(signal.last_real_bid_depth_usdc),
        "first_real_ask_depth_usdc": _opt(signal.first_real_ask_depth_usdc),
        "last_real_ask_depth_usdc": _opt(signal.last_real_ask_depth_usdc),
        "real_bid_depth_delta_usdc": _opt(signal.real_bid_depth_delta_usdc),
        "real_ask_depth_delta_usdc": _opt(signal.real_ask_depth_delta_usdc),
        "bid_price_velocity": _opt(signal.bid_price_velocity),
        "ask_price_velocity": _opt(signal.ask_price_velocity),
        "mid_price_velocity": _opt(signal.mid_price_velocity),
        "bid_size_consumption_rate": _opt(signal.bid_size_consumption_rate),
        "ask_size_consumption_rate": _opt(signal.ask_size_consumption_rate),
        "direction_score": str(signal.direction_score),
        "price_momentum": str(signal.price_momentum),
        "flow_imbalance": str(signal.flow_imbalance),
        "direction_label": signal.direction_label,
        "confidence": str(signal.confidence),
    }


class MarketMiscAggregator:
    def __init__(
        self,
        *,
        runtime: Any,
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
        serializer: AdminSerializer | None = None,
    ) -> None:
        self._runtime = runtime
        self._session_factory = (
            session_factory
            if session_factory is not None
            else (getattr(runtime, "db_session_factory", None) if runtime else None)
        )
        self._serializer = serializer or AdminSerializer.from_runtime(runtime)

    # ---------- shared helpers ----------
    def _account_snapshot(self) -> AccountSnapshot:
        store = getattr(self._runtime, "account_state_store", None) if self._runtime else None
        return store.snapshot() if store is not None else AccountSnapshot()

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        registry = getattr(self._runtime, "registry", None) if self._runtime else None
        return registry.snapshot() if registry is not None else MarketRegistrySnapshot(tuple())

    def _market_ws_snapshot(self, token_id: str) -> Any | None:
        worker = getattr(self._runtime, "market_ws_worker", None) if self._runtime else None
        return worker.snapshot(token_id) if worker is not None else None

    def _clob_client(self) -> Any:
        if self._runtime is None:
            raise RuntimeError("clob_client unavailable")
        return self._runtime.clob_client

    def _resolve_market(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> Market | None:
        registry = getattr(self._runtime, "registry", None) if self._runtime else None
        if registry is None:
            return None
        if condition_id is not None:
            m = registry.get_by_condition_id(condition_id)
            if m is not None:
                return m
        if token_id is not None:
            m = registry.get_by_token_id(token_id)
            if m is not None:
                return m
        if market_slug is not None:
            m = registry.get_by_slug(market_slug)
            if m is not None:
                return m
        return None

    def _publish_audit(self, event_type: DomainEventType, payload: dict[str, Any], reason: str) -> None:
        bus = getattr(self._runtime, "event_bus", None) if self._runtime else None
        if bus is None:
            return
        try:
            event = DomainEvent(
                trace_id=uuid4().hex,
                event_type=event_type,
                event_id=uuid4().hex,
                reason=reason,
                payload=dict(payload),
            )
            bus.publish_nowait(OutboxPriority.P3, event)
        except Exception:
            pass

    # ---------- list_markets ----------
    async def list_markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trading_status: str | None = None,
        fees_enabled: bool | None = None,
        fee_rate_bps_min: int | None = None,
        fee_rate_bps_max: int | None = None,
        maker_base_fee_bps_min: int | None = None,
        maker_base_fee_bps_max: int | None = None,
        taker_base_fee_bps_min: int | None = None,
        taker_base_fee_bps_max: int | None = None,
        sort_by: MarketFeeSortField | None = None,
        sort_direction: SortDirection = "desc",
    ) -> dict[str, Any]:
        registry = self._registry_snapshot()
        if registry.markets or self._session_factory is None:
            account = self._account_snapshot()
            markets = sort_markets(
                tuple(
                    market
                    for market in registry.markets
                    if (trading_status is None or market.trading_status.value == trading_status)
                    and _market_matches_fee_filters(
                        market,
                        fees_enabled=fees_enabled,
                        fee_rate_bps_min=fee_rate_bps_min,
                        fee_rate_bps_max=fee_rate_bps_max,
                        maker_base_fee_bps_min=maker_base_fee_bps_min,
                        maker_base_fee_bps_max=maker_base_fee_bps_max,
                        taker_base_fee_bps_min=taker_base_fee_bps_min,
                        taker_base_fee_bps_max=taker_base_fee_bps_max,
                    )
                ),
                sort_by=sort_by,
                sort_direction=sort_direction,
            )
            page = slice_sequence(markets, limit=limit, offset=offset)
            items = [
                self._serializer.market_view(
                    market,
                    account_snapshot=account,
                    registry_snapshot=registry,
                )
                for market in page.items
            ]
            return page_payload(
                RepositoryPage(items=tuple(items), total=len(markets), limit=page.limit, offset=page.offset),
                serializer=lambda item: item,
            )

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.market.list_markets_snapshot(
                limit=limit,
                offset=offset,
                trading_status=trading_status,
                fees_enabled=fees_enabled,
                fee_rate_bps_min=fee_rate_bps_min,
                fee_rate_bps_max=fee_rate_bps_max,
                maker_base_fee_bps_min=maker_base_fee_bps_min,
                maker_base_fee_bps_max=maker_base_fee_bps_max,
                taker_base_fee_bps_min=taker_base_fee_bps_min,
                taker_base_fee_bps_max=taker_base_fee_bps_max,
                sort_by=sort_by,
                sort_direction=sort_direction,
            )

        page = await with_repositories(self._session_factory, _query)
        account = self._account_snapshot()
        items = [
            self._serializer.market_view(
                market,
                account_snapshot=account,
                registry_snapshot=registry,
            )
            for market in page.items
        ]
        return page_payload(
            RepositoryPage(items=tuple(items), total=page.total, limit=page.limit, offset=page.offset),
            serializer=lambda item: item,
        )

    # ---------- orderbook / midpoint / prices_history ----------
    async def get_market_orderbook(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any] | None:
        market = self._resolve_market(
            market_slug=market_slug, condition_id=condition_id, token_id=token_id,
        )
        resolved_market_slug = market.market_slug if market is not None else market_slug
        resolved_condition_id = market.condition_id if market is not None else condition_id
        resolved_token_id = token_id
        if resolved_token_id is None:
            return None
        snapshot = self._market_ws_snapshot(resolved_token_id)
        source = "hot"
        if snapshot is None or _orderbook_has_no_quotes(snapshot):
            orderbook = await self._clob_client().get_orderbook(
                resolved_token_id,
                market_slug=resolved_market_slug,
                condition_id=resolved_condition_id,
            )
            snapshot = orderbook.to_snapshot()
            source = "rest"
        return self._serializer.market_orderbook(
            token_id=resolved_token_id,
            condition_id=resolved_condition_id,
            market_slug=resolved_market_slug,
            orderbook=snapshot,
            source=source,
        )

    async def get_market_midpoint(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any] | None:
        market = self._resolve_market(
            market_slug=market_slug, condition_id=condition_id, token_id=token_id,
        )
        resolved_market_slug = market.market_slug if market is not None else market_slug
        resolved_condition_id = market.condition_id if market is not None else condition_id
        resolved_token_id = token_id
        if resolved_token_id is None:
            return None
        snapshot = self._market_ws_snapshot(resolved_token_id)
        source = "hot"
        if snapshot is not None and snapshot.best_bid is not None and snapshot.best_ask is not None:
            midpoint = (snapshot.best_bid + snapshot.best_ask) / Decimal("2")
        else:
            try:
                midpoint = await self._clob_client().get_midpoint(resolved_token_id)
            except PolymarketClientError as exc:
                if exc.status_code != 404:
                    raise
                midpoint = None
            source = "rest"
        return self._serializer.market_midpoint(
            token_id=resolved_token_id,
            condition_id=resolved_condition_id,
            market_slug=resolved_market_slug,
            midpoint=midpoint,
            orderbook=snapshot,
            source=source,
        )

    async def get_market_prices_history(
        self,
        *,
        token_id: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        interval: str | None = None,
        fidelity: int | None = None,
    ) -> dict[str, Any]:
        history = await self._clob_client().get_prices_history(
            token_id,
            start_ts=start_ts,
            end_ts=end_ts,
            interval=interval,
            fidelity=fidelity,
        )
        return self._serializer.market_prices_history(
            token_id=token_id,
            history=history,
            interval=interval,
            fidelity=fidelity,
        )

    # ---------- orderbook direction (single + multi window) ----------
    async def get_orderbook_direction(
        self,
        *,
        token_id: str,
        window_seconds: float = 10.0,
    ) -> dict[str, Any]:
        store = getattr(self._runtime, "orderbook_delta_store", None) if self._runtime else None
        if store is None:
            return {
                "token_id": token_id,
                "window_seconds": window_seconds,
                "signal": None,
                "reason": "store_unavailable",
            }
        signal = store.direction_signal(token_id, window_seconds=window_seconds)
        if signal is None:
            payload: dict[str, Any] = {
                "token_id": token_id,
                "window_seconds": window_seconds,
                "signal": None,
                "reason": "insufficient_samples",
                "tracked_tokens": len(store.tracked_tokens()),
            }
        else:
            payload = {"token_id": token_id, **_serialize_direction_signal(signal)}
        self._publish_audit(
            DomainEventType.ORDERBOOK_DIRECTION_QUERIED, payload, "admin_direction_query",
        )
        return payload

    async def get_orderbook_direction_multi(
        self,
        *,
        token_id: str,
        windows: tuple[float, ...],
    ) -> dict[str, Any]:
        store = getattr(self._runtime, "orderbook_delta_store", None) if self._runtime else None
        if store is None:
            return {
                "token_id": token_id,
                "windows": list(windows),
                "signals": [],
                "reason": "store_unavailable",
            }
        signals_payload: list[dict[str, Any]] = []
        for w in windows:
            signal = store.direction_signal(token_id, window_seconds=w)
            if signal is None:
                signals_payload.append({
                    "window_seconds": w,
                    "signal": None,
                    "reason": "insufficient_samples",
                })
            else:
                signals_payload.append(_serialize_direction_signal(signal))
        payload: dict[str, Any] = {
            "token_id": token_id,
            "windows_requested": list(windows),
            "tracked_tokens": len(store.tracked_tokens()),
            "signals": signals_payload,
        }
        self._publish_audit(
            DomainEventType.ORDERBOOK_DIRECTION_QUERIED,
            payload,
            "admin_direction_query_multi",
        )
        return payload

    # ---------- orderbook history (DB) ----------
    async def list_orderbook_history(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        token_id: str | None = None,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        if self._session_factory is None:
            page: RepositoryPage[Any] = RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer.orderbook)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.orderbook.list_snapshots(
                limit=limit,
                offset=offset,
                token_id=token_id,
                condition_id=condition_id,
                time_range=time_range,
            )

        page = await with_repositories(self._session_factory, _query)
        return page_payload(page, serializer=self._serializer.orderbook)

    # ---------- liquidity / impact (in-memory WS only) ----------
    def get_market_liquidity(
        self,
        *,
        token_id: str,
        condition_id: str | None = None,
        market_slug: str | None = None,
        depth_ticks: int = 5,
    ) -> dict[str, Any] | None:
        snapshot = self._market_ws_snapshot(token_id)
        if snapshot is None or _orderbook_has_no_quotes(snapshot):
            return None
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        received_ms = int(snapshot.received_at.timestamp() * 1000)
        age_ms = now_ms - received_ms

        active_bids = sorted(
            (lv for lv in snapshot.bids if lv.size > Decimal("0")),
            key=lambda lv: lv.price,
            reverse=True,
        )
        active_asks = sorted(
            (lv for lv in snapshot.asks if lv.size > Decimal("0")),
            key=lambda lv: lv.price,
        )

        def _depth_tiers(levels: list, max_ticks: int) -> list[dict[str, str]]:
            tiers = []
            cumulative_size = Decimal("0")
            cumulative_usdc = Decimal("0")
            for i, level in enumerate(levels[:max_ticks]):
                cumulative_size += level.size
                cumulative_usdc += level.price * level.size
                tiers.append({
                    "tick": str(i + 1),
                    "price": decimal_text(level.price),
                    "size": decimal_text(level.size),
                    "cumulative_size": decimal_text(cumulative_size),
                    "cumulative_usdc": decimal_text(cumulative_usdc),
                })
            return tiers

        def _vwap_side(levels: list, max_ticks: int) -> Decimal | None:
            total_size = Decimal("0")
            total_pv = Decimal("0")
            for level in levels[:max_ticks]:
                total_size += level.size
                total_pv += level.price * level.size
            if total_size == Decimal("0"):
                return None
            return total_pv / total_size

        vwap_bid = _vwap_side(active_bids, depth_ticks)
        vwap_ask = _vwap_side(active_asks, depth_ticks)
        vwap_mid = (
            (vwap_bid + vwap_ask) / Decimal("2")
            if vwap_bid is not None and vwap_ask is not None
            else None
        )
        simple_mid = (
            (snapshot.best_bid + snapshot.best_ask) / Decimal("2")
            if snapshot.best_bid is not None and snapshot.best_ask is not None
            else None
        )
        return {
            "token_id": token_id,
            "condition_id": condition_id,
            "market_slug": market_slug,
            "snapshot_age_ms": age_ms,
            "best_bid": decimal_text(snapshot.best_bid) if snapshot.best_bid is not None else None,
            "best_ask": decimal_text(snapshot.best_ask) if snapshot.best_ask is not None else None,
            "best_bid_size": decimal_text(snapshot.best_bid_size) if snapshot.best_bid_size is not None else None,
            "best_ask_size": decimal_text(snapshot.best_ask_size) if snapshot.best_ask_size is not None else None,
            "spread": decimal_text(snapshot.spread) if snapshot.spread is not None else None,
            "effective_spread_bps": (
                decimal_text(snapshot.spread / simple_mid * Decimal("10000"))
                if snapshot.spread is not None and simple_mid is not None and simple_mid > Decimal("0")
                else None
            ),
            "vwap_mid": decimal_text(vwap_mid) if vwap_mid is not None else None,
            "vwap_bid": decimal_text(vwap_bid) if vwap_bid is not None else None,
            "vwap_ask": decimal_text(vwap_ask) if vwap_ask is not None else None,
            "bid_depth": _depth_tiers(active_bids, depth_ticks),
            "ask_depth": _depth_tiers(active_asks, depth_ticks),
            "total_bid_size": decimal_text(
                sum((level.size for level in active_bids[:depth_ticks]), Decimal("0"))
            ),
            "total_ask_size": decimal_text(
                sum((level.size for level in active_asks[:depth_ticks]), Decimal("0"))
            ),
        }

    def get_market_impact(
        self,
        *,
        token_id: str,
        size_usdc: Decimal,
        condition_id: str | None = None,
        market_slug: str | None = None,
    ) -> dict[str, Any] | None:
        snapshot = self._market_ws_snapshot(token_id)
        if snapshot is None or _orderbook_has_no_quotes(snapshot):
            return None
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        received_ms = int(snapshot.received_at.timestamp() * 1000)
        age_ms = now_ms - received_ms
        active_asks = sorted(
            (lv for lv in snapshot.asks if lv.size > Decimal("0")),
            key=lambda lv: lv.price,
        )
        remaining_usdc = size_usdc
        total_shares = Decimal("0")
        total_cost = Decimal("0")
        for level in active_asks:
            if remaining_usdc <= Decimal("0"):
                break
            level_usdc = level.price * level.size
            fill_usdc = min(remaining_usdc, level_usdc)
            fill_shares = fill_usdc / level.price
            total_shares += fill_shares
            total_cost += fill_usdc
            remaining_usdc -= fill_usdc
        fillable_usdc = size_usdc - remaining_usdc
        avg_price = total_cost / total_shares if total_shares > Decimal("0") else None
        best_ask = snapshot.best_ask
        impact_bps = None
        if avg_price is not None and best_ask is not None and best_ask > Decimal("0"):
            impact_bps = decimal_text(
                (avg_price - best_ask) / best_ask * Decimal("10000")
            )
        return {
            "token_id": token_id,
            "condition_id": condition_id,
            "market_slug": market_slug,
            "snapshot_age_ms": age_ms,
            "requested_usdc": decimal_text(size_usdc),
            "fillable_usdc": decimal_text(fillable_usdc),
            "unfillable_usdc": decimal_text(remaining_usdc),
            "estimated_shares": decimal_text(total_shares) if total_shares > Decimal("0") else None,
            "estimated_avg_price": decimal_text(avg_price) if avg_price is not None else None,
            "best_ask": decimal_text(best_ask) if best_ask is not None else None,
            "price_impact_bps": impact_bps,
            "fully_fillable": remaining_usdc == Decimal("0"),
        }

    # ---------- orderbook depth ----------
    def orderbook_depth_snapshot(
        self,
        *,
        token_id: str,
        windows_s: tuple[float, ...] = (2.0, 3.0, 5.0, 10.0),
    ) -> dict[str, Any]:
        if self._runtime is None or self._runtime.market_ws_worker is None:
            return {"token_id": token_id, "available": False, "reason": "market_ws_unavailable"}
        ws = self._runtime.market_ws_worker
        current = ws.snapshot(token_id)
        if current is None:
            return {"token_id": token_id, "available": False, "reason": "snapshot_not_found"}

        derived_store = getattr(self._runtime, "orderbook_derived_store", None)
        derived = derived_store.get(token_id) if derived_store is not None else None
        if derived is None:
            return {"token_id": token_id, "available": False, "reason": "derived_not_ready"}

        market_obj = (
            self._runtime.registry.get_by_token_id(token_id)
            if self._runtime.registry else None
        )
        classification = _classify_market(market_obj)

        our_position_shares = Decimal("0")
        our_position_cost = Decimal("0")
        our_resting_buy_size = Decimal("0")
        our_resting_buy_usdc = Decimal("0")
        our_resting_sell_size = Decimal("0")
        our_resting_sell_usdc = Decimal("0")
        if self._runtime.paper_ledger is not None:
            shares = self._runtime.paper_ledger.positions.get(token_id, Decimal("0"))
            our_position_shares = shares
            our_position_cost = self._runtime.paper_ledger.cost_basis_usdc.get(token_id, Decimal("0"))
        account = self._account_snapshot() if self._runtime.account_state_store else None
        if account is not None:
            for o in account.open_orders:
                if o.token_id != token_id:
                    continue
                remaining = (o.size - (o.filled_size or Decimal("0")))
                if remaining <= Decimal("0"):
                    continue
                usdc = remaining * o.price
                if o.side.value.lower() == "buy":
                    our_resting_buy_size += remaining
                    our_resting_buy_usdc += usdc
                else:
                    our_resting_sell_size += remaining
                    our_resting_sell_usdc += usdc

        history = getattr(self._runtime, "orderbook_history_buffer", None) if self._runtime else None
        return {
            "token_id": token_id,
            "condition_id": current.condition_id,
            "market_slug": current.market_slug,
            "available": True,
            "received_at": current.received_at.isoformat(),
            "best_bid": str(current.best_bid) if current.best_bid is not None else None,
            "best_ask": str(current.best_ask) if current.best_ask is not None else None,
            "spread": str(current.spread) if current.spread is not None else None,
            "liquidity_metrics": _serialize_liquidity_metrics(derived),
            "bid": _serialize_side_summary(derived.bid_side),
            "ask": _serialize_side_summary(derived.ask_side),
            "ours": {
                "position_shares": str(our_position_shares),
                "position_cost_usdc": str(our_position_cost),
                "resting_buy_size": str(our_resting_buy_size),
                "resting_buy_usdc": str(our_resting_buy_usdc),
                "resting_sell_size": str(our_resting_sell_size),
                "resting_sell_usdc": str(our_resting_sell_usdc),
            },
            "windows": [_serialize_window_delta(w) for w in derived.windows],
            "history_buffer_samples": history.sample_count(token_id) if history else 0,
            "classification": classification,
            "derived_computed_at": derived.computed_at.isoformat(),
        }

    # ---------- liquidity summary (global) ----------
    def liquidity_summary_snapshot(self, *, top_n: int = 20) -> dict[str, Any]:
        if self._runtime is None or self._runtime.market_ws_worker is None:
            return {"available": False, "reason": "market_ws_unavailable"}
        D = Decimal
        WHALE = D("50")
        ws = self._runtime.market_ws_worker
        states = getattr(ws, "_states", {})
        tracked_tokens = 0
        total_bid_usdc = D("0")
        total_ask_usdc = D("0")
        whale_bid_usdc = D("0")
        whale_ask_usdc = D("0")
        whale_bid_count = 0
        whale_ask_count = 0
        by_classification: dict[str, dict[str, Any]] = {}
        market_depths: list[dict[str, Any]] = []
        for token_id, state in states.items():
            snap = getattr(state, "snapshot", None)
            if snap is None:
                continue
            tracked_tokens += 1
            mbid = D("0"); mask = D("0"); mw_bc = mw_ac = 0
            mw_bu = D("0"); mw_au = D("0")
            for lvl in snap.bids:
                u = lvl.size * lvl.price
                mbid += u
                if u >= WHALE:
                    mw_bc += 1
                    mw_bu += u
            for lvl in snap.asks:
                u = lvl.size * lvl.price
                mask += u
                if u >= WHALE:
                    mw_ac += 1
                    mw_au += u
            total_bid_usdc += mbid
            total_ask_usdc += mask
            whale_bid_usdc += mw_bu
            whale_ask_usdc += mw_au
            whale_bid_count += mw_bc
            whale_ask_count += mw_ac
            market_obj = self._runtime.registry.get_by_token_id(token_id) if self._runtime.registry else None
            cls = _classify_market(market_obj)
            kind = cls.get("sports_market_type") or cls.get("inferred_kind") or "unknown"
            agg = by_classification.setdefault(kind, {"markets": 0, "bid_usdc": D("0"), "ask_usdc": D("0")})
            agg["markets"] += 1
            agg["bid_usdc"] += mbid
            agg["ask_usdc"] += mask
            market_depths.append({
                "token_id": token_id[:24] + "...",
                "market_slug": snap.market_slug,
                "total_usdc": str(mbid + mask),
                "bid_usdc": str(mbid),
                "ask_usdc": str(mask),
            })
        for row in market_depths:
            b = D(row["bid_usdc"])
            a = D(row["ask_usdc"])
            total = b + a
            if total > 0:
                row["bid_share"] = str(round(b / total, 4))
                row["ask_share"] = str(round(a / total, 4))
                row["bid_to_ask_ratio"] = str(round(b / a, 4)) if a > 0 else "inf"
            else:
                row["bid_share"] = None
                row["ask_share"] = None
                row["bid_to_ask_ratio"] = None
        market_depths.sort(key=lambda r: float(r["total_usdc"]), reverse=True)
        cls_out = []
        for kind, v in by_classification.items():
            b, a = v["bid_usdc"], v["ask_usdc"]
            total = b + a
            cls_out.append({
                "classification": kind,
                "market_count": v["markets"],
                "bid_usdc": str(b),
                "ask_usdc": str(a),
                "total_usdc": str(total),
                "bid_share": str(round(b / total, 4)) if total > 0 else None,
                "ask_share": str(round(a / total, 4)) if total > 0 else None,
            })
        cls_out.sort(key=lambda r: float(r["total_usdc"]), reverse=True)
        global_total = total_bid_usdc + total_ask_usdc
        global_bid_share = str(round(total_bid_usdc / global_total, 4)) if global_total > 0 else None
        global_ask_share = str(round(total_ask_usdc / global_total, 4)) if global_total > 0 else None
        whale_global = whale_bid_usdc + whale_ask_usdc
        whale_bid_share = str(round(whale_bid_usdc / whale_global, 4)) if whale_global > 0 else None
        return {
            "available": True,
            "tracked_tokens": tracked_tokens,
            "total_bid_usdc": str(total_bid_usdc),
            "total_ask_usdc": str(total_ask_usdc),
            "total_usdc": str(global_total),
            "bid_share": global_bid_share,
            "ask_share": global_ask_share,
            "whale_threshold_usdc": str(WHALE),
            "whale_bid_count": whale_bid_count,
            "whale_ask_count": whale_ask_count,
            "whale_bid_usdc": str(whale_bid_usdc),
            "whale_ask_usdc": str(whale_ask_usdc),
            "whale_total_usdc": str(whale_global),
            "whale_bid_share": whale_bid_share,
            "by_classification": cls_out,
            "top_n_deepest": market_depths[:top_n],
        }

    # ---------- event bundle ----------
    def event_bundle_snapshot(self, *, event_slug: str) -> dict[str, Any]:
        if self._runtime is None or self._runtime.registry is None:
            return {"available": False, "reason": "registry_unavailable"}
        D = Decimal
        registry = self._runtime.registry.snapshot()
        matched = [m for m in registry.markets if (m.event_slug or "") == event_slug]
        if not matched:
            return {"available": False, "reason": "event_not_found", "event_slug": event_slug}
        ws = self._runtime.market_ws_worker
        markets_out = []
        total_usdc_all = D("0")
        for m in matched:
            cls = _classify_market(m)
            tokens_info = []
            market_total = D("0")
            for tok in (m.token_ids or ()):
                snap = ws.snapshot(tok) if ws else None
                if snap is None:
                    tokens_info.append({"token_id": tok, "snapshot": "missing"})
                    continue
                bid_u = sum((lvl.size * lvl.price for lvl in snap.bids), D("0"))
                ask_u = sum((lvl.size * lvl.price for lvl in snap.asks), D("0"))
                market_total += (bid_u + ask_u)
                outcome = next((o for o in m.outcomes if o.token_id == tok), None)
                tok_total = bid_u + ask_u
                tokens_info.append({
                    "token_id": tok,
                    "outcome": outcome.outcome if outcome else None,
                    "best_bid": str(snap.best_bid) if snap.best_bid else None,
                    "best_ask": str(snap.best_ask) if snap.best_ask else None,
                    "bid_usdc": str(bid_u),
                    "ask_usdc": str(ask_u),
                    "total_usdc": str(tok_total),
                    "bid_share": str(round(bid_u / tok_total, 4)) if tok_total > 0 else None,
                    "ask_share": str(round(ask_u / tok_total, 4)) if tok_total > 0 else None,
                })
            total_usdc_all += market_total
            asks_for_vig: list[Decimal] = []
            for ti in tokens_info:
                if ti.get("best_ask"):
                    try:
                        asks_for_vig.append(D(ti["best_ask"]))
                    except Exception:
                        pass
            vig_info = None
            if len(asks_for_vig) == 2:
                ask_sum = sum(asks_for_vig)
                vig_info = {
                    "sum_best_ask": str(ask_sum),
                    "overround_bps": str(round((ask_sum - D("1")) * D("10000"), 2)),
                    "arbitrage_opportunity": ask_sum < D("0.99"),
                }
            markets_out.append({
                "condition_id": m.condition_id,
                "market_slug": m.market_slug,
                "classification": cls,
                "tokens": tokens_info,
                "total_usdc": str(market_total),
                "vig": vig_info,
            })
        return {
            "available": True,
            "event_slug": event_slug,
            "market_count": len(matched),
            "total_usdc": str(total_usdc_all),
            "markets": markets_out,
        }

    # ---------- market data health ----------
    def market_data_health_snapshot(
        self,
        *,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any]:
        if self._runtime is None:
            return {"available": False, "reason": "runtime_unavailable"}
        registry = self._registry_snapshot()
        market = None
        if condition_id is not None:
            market = next((m for m in registry.markets if m.condition_id == condition_id), None)
        elif token_id is not None:
            for m in registry.markets:
                if token_id in (m.token_ids or ()):
                    market = m
                    break
        if market is None:
            return {
                "available": False,
                "reason": "market_not_found",
                "condition_id": condition_id,
                "token_id": token_id,
            }

        now = datetime.now(timezone.utc)
        result: dict[str, Any] = {
            "available": True,
            "condition_id": market.condition_id,
            "market_slug": market.market_slug,
            "event_slug": market.event_slug,
            "classification": _classify_market(market),
        }
        ws_info: dict[str, Any] = {}
        if self._runtime.market_ws_worker is not None:
            ws = self._runtime.market_ws_worker
            ws_status = ws.status_snapshot(include_subscriptions=True)
            tokens = tuple(market.token_ids or ())
            ws_info["subscribed_tokens"] = [t for t in tokens if t in ws_status.subscribed_token_ids]
            ws_info["unsubscribed_tokens"] = [t for t in tokens if t not in ws_status.subscribed_token_ids]
            snap_ages = {}
            for t in tokens:
                snap = ws.snapshot(t)
                if snap:
                    snap_ages[t] = int((now - snap.received_at).total_seconds() * 1000)
                else:
                    snap_ages[t] = None
            ws_info["snapshot_age_ms_per_token"] = snap_ages
        result["market_ws"] = ws_info

        live_info: dict[str, Any] = {}
        meta_store = self._runtime.market_metadata_store
        if meta_store is not None:
            rec = next((r for r in meta_store.records() if r.condition_id == market.condition_id), None)
            if rec is None:
                live_info["has_state"] = False
            else:
                age_ms = int((now - rec.updated_at).total_seconds() * 1000)
                live_info = {
                    "has_state": bool(rec.live_state_payload),
                    "source": rec.source,
                    "signal_allowed": rec.live_state_signal_allowed,
                    "age_ms": age_ms,
                }
                if rec.live_state_payload and isinstance(rec.live_state_payload, dict):
                    lg = rec.live_state_payload.get("live_game", {}) or {}
                    live_info["state_summary"] = {
                        "status": lg.get("status"),
                        "period": lg.get("period"),
                        "home_score": lg.get("home_score"),
                        "away_score": lg.get("away_score"),
                        "seconds_remaining": lg.get("seconds_remaining"),
                        "sport": lg.get("sport"),
                    }
        result["live_state"] = live_info

        inplay_info: list[dict[str, Any]] = []
        sport = (live_info.get("state_summary") or {}).get("sport") if isinstance(live_info, dict) else None
        if self._runtime.sports_live_state_client is not None:
            try:
                all_status = self._runtime.sports_live_state_client.source_detail_status()
                if sport:
                    inplay_info = [s for s in all_status if s.get("sport") == sport]
                else:
                    inplay_info = all_status
            except Exception as exc:
                inplay_info = [{"error": str(exc)[:120]}]
        result["sources_per_sport"] = inplay_info

        pg_worker = getattr(self._runtime, "pregame_worker", None)
        if pg_worker is not None and hasattr(pg_worker, "status_snapshot"):
            result["pregame"] = dict(pg_worker.status_snapshot())
        else:
            result["pregame"] = {"enabled": False}

        return result


    async def market_trade_tape(
        self: MarketMiscAggregator,
        *,
        condition_id: str,
        limit: int = 50,
    ) -> dict[str, object]:
        """Polymarket 公开 trade tape — 该 market 最近 N 笔实际成交。

        含每笔 side(BUY/SELL) / size / price / timestamp / wallet(proxyWallet)，
        是 OFI 之外**真正订单流**的来源（OFI 只算盘口变化，trade tape 是实成交）。
        """
        import time as _time
        import os as _os

        import httpx as _httpx

        cache_key = f"{condition_id}:{limit}"
        cached = _TRADE_TAPE_CACHE.get(cache_key)
        if cached and (_time.time() - cached[0]) < 2:
            return cached[1]
        url = f"https://data-api.polymarket.com/trades?market={condition_id}&limit={limit}"
        proxy = _os.environ.get("HTTPS_PROXY") or _os.environ.get("https_proxy")
        try:
            mounts = (
                {"https://": _httpx.AsyncHTTPTransport(proxy=proxy)} if proxy else None
            )
            async with _httpx.AsyncClient(mounts=mounts, trust_env=False, timeout=8) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    return {"error": f"http {r.status_code}", "trades": []}
                data = r.json()
                trades = data if isinstance(data, list) else (data.get("data") or [])
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "trades": []}

        from collections import Counter as _Counter

        buys = sells = 0
        buy_notional = sell_notional = Decimal("0")
        wallets: _Counter[str] = _Counter()
        large_trades: list[dict[str, Any]] = []
        latest_ts = 0
        normalized: list[dict[str, Any]] = []
        for t in trades:
            if not isinstance(t, dict):
                continue
            side = (t.get("side") or "").upper()
            try:
                size = Decimal(str(t.get("size", 0)))
                price = Decimal(str(t.get("price", 0)))
            except Exception:  # noqa: BLE001
                continue
            notional = size * price
            ts = int(t.get("timestamp", 0))
            latest_ts = max(latest_ts, ts)
            wallet = (t.get("proxyWallet") or "")[:10]
            wallets[wallet] += 1
            if side == "BUY":
                buys += 1
                buy_notional += notional
            elif side == "SELL":
                sells += 1
                sell_notional += notional
            entry = {
                "side": side,
                "size": str(size),
                "price": str(price),
                "notional_usdc": str(notional.quantize(Decimal("0.01"))),
                "timestamp": ts,
                "wallet": wallet,
                "outcome": t.get("outcome"),
                "slug": t.get("slug"),
            }
            if notional >= Decimal("100"):
                large_trades.append(entry)
            normalized.append(entry)
        total_count = buys + sells
        notional_sum = buy_notional + sell_notional
        result = {
            "condition_id": condition_id,
            "trades_count": total_count,
            "buy_count": buys,
            "sell_count": sells,
            "buy_notional_usdc": str(buy_notional.quantize(Decimal("0.01"))),
            "sell_notional_usdc": str(sell_notional.quantize(Decimal("0.01"))),
            "net_flow_usdc": str((buy_notional - sell_notional).quantize(Decimal("0.01"))),
            "buy_flow_pct": (
                round(float(buy_notional / notional_sum * 100), 1) if notional_sum > 0 else None
            ),
            "avg_trade_size_usdc": (
                str((notional_sum / total_count).quantize(Decimal("0.01")))
                if total_count > 0 else "0"
            ),
            "large_trades_count": len(large_trades),
            "unique_wallets": len(wallets),
            "top_wallets": dict(wallets.most_common(5)),
            "latest_trade_at": latest_ts,
            "trades": normalized[:20],
            "large_trades": large_trades[:10],
        }
        _TRADE_TAPE_CACHE[cache_key] = (_time.time(), result)
        if len(_TRADE_TAPE_CACHE) > 200:
            _TRADE_TAPE_CACHE.clear()
        return result

    def derived_metrics_snapshot(
        self: MarketMiscAggregator, *, market_slug: str | None = None
    ) -> dict[str, object]:
        """派生量化指标：基于已有时序数据计算高阶统计/特征。

        每个 market：odds_volatility / odds_drift_rate / market_efficiency / trend。
        每个持仓：holding + max_drawdown + price_trend → quality_score 0-100。
        """
        from statistics import mean as _mean, stdev as _stdev

        from polymarket_trader.pipeline.decision.worker import get_odds_drift_store

        store = get_odds_drift_store()
        if not store:
            return {"markets": [], "summary": {"tracked": 0}}

        results: list[dict] = []
        for slug, dq in store.items():
            if market_slug and slug != market_slug:
                continue
            samples = list(dq)
            if len(samples) < 3:
                continue
            recent = samples[-60:]
            home_probs = [float(s["ml_home_p"]) for s in recent if s.get("ml_home_p") is not None]
            away_probs = [float(s["ml_away_p"]) for s in recent if s.get("ml_away_p") is not None]
            home_std = round(_stdev(home_probs), 4) if len(home_probs) >= 2 else 0
            away_std = round(_stdev(away_probs), 4) if len(away_probs) >= 2 else 0
            short_window = samples[-15:] if len(samples) >= 15 else samples
            ml_home_drift = None
            if (
                len(short_window) >= 2
                and short_window[0].get("ml_home_p")
                and short_window[-1].get("ml_home_p")
            ):
                ml_home_drift = round(
                    float(short_window[-1]["ml_home_p"]) - float(short_window[0]["ml_home_p"]), 4
                )
            vigs = []
            for s in recent:
                hp, ap = s.get("ml_home_p"), s.get("ml_away_p")
                if hp is not None and ap is not None:
                    vigs.append(float(hp) + float(ap) - 1.0)
            vig_mean = round(_mean(vigs), 4) if vigs else None
            vig_std = round(_stdev(vigs), 4) if len(vigs) >= 2 else None
            efficiency = None
            if vig_std is not None and vig_std > 0:
                efficiency = round(1.0 / (1.0 + vig_std * 10), 3)
            trend = "stable"
            if ml_home_drift is not None:
                if ml_home_drift > 0.02:
                    trend = "home_strengthening"
                elif ml_home_drift < -0.02:
                    trend = "away_strengthening"
            results.append({
                "market_slug": slug,
                "samples_used": len(recent),
                "odds_volatility": {
                    "ml_home_std": home_std,
                    "ml_away_std": away_std,
                    "max_volatility": max(home_std, away_std),
                },
                "drift_rate": {"ml_home_drift_30s": ml_home_drift, "trend": trend},
                "market_efficiency": {
                    "vig_mean": vig_mean,
                    "vig_std": vig_std,
                    "efficiency_score": efficiency,
                },
                "last_sample": samples[-1],
            })

        position_quality: list[dict] = []
        runtime = self._runtime
        if runtime and runtime.paper_ledger:
            ledger = runtime.paper_ledger
            for tok, shares in ledger.positions.items():
                if shares <= Decimal("0"):
                    continue
                cost = ledger.cost_basis_usdc.get(tok, Decimal("0"))
                entry_price = cost / shares if shares > 0 else Decimal("0")
                history = ledger.position_price_history.get(tok, [])
                first_fill = ledger.first_fill_at.get(tok)
                holding_seconds = (
                    (datetime.now(timezone.utc) - first_fill).total_seconds()
                    if first_fill else 0
                )
                max_loss = ledger.max_unrealized_loss.get(tok, Decimal("0"))
                price_trend = "unknown"
                drift_pct = None
                if len(history) >= 2:
                    try:
                        first_bid = Decimal(str(history[0][1]))
                        last_bid = Decimal(str(history[-1][1]))
                        if first_bid > 0:
                            drift_pct = round(float((last_bid - first_bid) / first_bid * 100), 2)
                            if drift_pct > 2:
                                price_trend = "rising"
                            elif drift_pct < -2:
                                price_trend = "falling"
                            else:
                                price_trend = "stable"
                    except Exception:  # noqa: BLE001
                        pass
                quality = 50
                if max_loss >= Decimal("0"):
                    quality += 20
                if price_trend == "rising":
                    quality += 20
                elif price_trend == "falling":
                    quality -= 20
                if holding_seconds > 14400:
                    quality -= 20
                quality = max(0, min(100, quality))
                position_quality.append({
                    "token_id": tok[:32],
                    "shares": str(shares),
                    "entry_price": str(entry_price.quantize(Decimal("0.0001"))),
                    "holding_seconds": round(holding_seconds, 0),
                    "holding_hours": round(holding_seconds / 3600, 2),
                    "max_drawdown_usdc": str(max_loss),
                    "price_trend": price_trend,
                    "drift_pct": drift_pct,
                    "quality_score": quality,
                })

        all_vols = [
            r["odds_volatility"]["max_volatility"]
            for r in results if r["odds_volatility"]["max_volatility"]
        ]
        all_effs = [
            r["market_efficiency"]["efficiency_score"]
            for r in results if r["market_efficiency"]["efficiency_score"]
        ]
        all_quality = [p["quality_score"] for p in position_quality]
        return {
            "markets_analyzed": len(results),
            "positions_count": len(position_quality),
            "summary": {
                "avg_odds_volatility": round(_mean(all_vols), 4) if all_vols else None,
                "max_odds_volatility": max(all_vols) if all_vols else None,
                "avg_market_efficiency": round(_mean(all_effs), 3) if all_effs else None,
                "avg_position_quality": round(_mean(all_quality), 1) if all_quality else None,
            },
            "markets": sorted(
                results, key=lambda x: -x["odds_volatility"]["max_volatility"]
            )[:30],
            "position_quality": position_quality,
        }

    def odds_drift_snapshot(
        self: MarketMiscAggregator,
        *,
        market_slug: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        """Goalserve 赔率漂移时序（每 market 5s 采样，最多保留 200 点）。"""
        from polymarket_trader.pipeline.decision.worker import get_odds_drift_store

        store = get_odds_drift_store()
        if market_slug:
            samples = list(store.get(market_slug, []))[-limit:]
            if not samples:
                return {"market_slug": market_slug, "samples": [], "samples_count": 0}
            first, last = samples[0], samples[-1]
            try:
                first_at = datetime.fromisoformat(first["at"])
                last_at = datetime.fromisoformat(last["at"])
                duration_s = (last_at - first_at).total_seconds()
            except Exception:  # noqa: BLE001
                duration_s = 0

            def _diff(key: str) -> float | None:
                a, b = first.get(key), last.get(key)
                if a is None or b is None:
                    return None
                return round(float(b) - float(a), 4)

            return {
                "market_slug": market_slug,
                "samples_count": len(samples),
                "duration_seconds": round(duration_s, 1),
                "first_sample": first,
                "last_sample": last,
                "ml_home_drift": _diff("ml_home_p"),
                "ml_away_drift": _diff("ml_away_p"),
                "tt_over_drift": _diff("tt_over_p"),
                "sp_home_drift": _diff("sp_home_p"),
                "samples": samples,
            }
        summary = []
        for slug, dq in store.items():
            samples = list(dq)
            if not samples:
                continue
            summary.append({
                "market_slug": slug,
                "samples_count": len(samples),
                "first_at": samples[0]["at"],
                "last_at": samples[-1]["at"],
            })
        summary.sort(key=lambda x: x["last_at"], reverse=True)
        return {"tracked_markets": len(summary), "summary": summary[:50]}

    def arbitrage_snapshot(self) -> dict[str, object]:
        """同 event 跨盘口套利检测——隐含概率和应该 ≤ 1 + vig。"""
        runtime = self._runtime
        if (
            runtime is None or runtime.registry is None or runtime.market_ws_worker is None
        ):
            return {"events": [], "checked_count": 0}
        from collections import defaultdict as _defaultdict

        registry = runtime.registry
        ws = runtime.market_ws_worker
        markets = registry.snapshot().markets
        by_event: dict[str, list] = _defaultdict(list)
        for m in markets:
            if not m.event_slug or m.trading_status != "eligible":
                continue
            by_event[m.event_slug].append(m)
        results: list[dict] = []
        for event_slug, ms in by_event.items():
            if len(ms) < 2:
                continue
            for market in ms:
                if len(market.token_ids) < 2:
                    continue
                ask_sum = Decimal("0")
                bid_sum = Decimal("0")
                outcomes_data: list[dict] = []
                valid = True
                for token_id, outcome_label in zip(
                    market.token_ids, [o.outcome for o in market.outcomes], strict=False,
                ):
                    ob = ws.snapshot(token_id)
                    if ob is None or ob.best_ask is None:
                        valid = False
                        break
                    ask_sum += ob.best_ask
                    if ob.best_bid is not None:
                        bid_sum += ob.best_bid
                    outcomes_data.append({
                        "outcome": outcome_label,
                        "best_ask": str(ob.best_ask),
                        "best_bid": str(ob.best_bid) if ob.best_bid else None,
                    })
                if not valid:
                    continue
                arb_signal = None
                if ask_sum < Decimal("1.0"):
                    arb_signal = "buy_all_arbitrage"
                elif ask_sum > Decimal("1.20"):
                    arb_signal = "high_vig_anomaly"
                if arb_signal or (Decimal("0.95") <= ask_sum <= Decimal("1.10")):
                    results.append({
                        "event_slug": event_slug,
                        "market_slug": market.market_slug,
                        "condition_id": market.condition_id[:10],
                        "outcomes_count": len(outcomes_data),
                        "ask_sum": str(ask_sum.quantize(Decimal("0.0001"))),
                        "bid_sum": (
                            str(bid_sum.quantize(Decimal("0.0001"))) if bid_sum > 0 else None
                        ),
                        "vig_pct": str(((ask_sum - Decimal("1")) * 100).quantize(Decimal("0.01"))),
                        "arb_signal": arb_signal,
                        "outcomes": outcomes_data,
                    })
        results.sort(key=lambda x: float(x["vig_pct"]))
        return {
            "checked_events": len(by_event),
            "results_count": len(results),
            "arbitrage_opportunities": [
                r for r in results if r["arb_signal"] == "buy_all_arbitrage"
            ],
            "anomalies": [r for r in results if r["arb_signal"] == "high_vig_anomaly"],
            "all_results": results[:30],
        }


    # 挂载到 class
# ===== module-level cache for trade tape =====
# 2s cache 防止 admin 反复调爆 Polymarket data-api（无明示限速但避免被 ban）
_TRADE_TAPE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


# 把 4 个 runtime/* 类方法补到 MarketMiscAggregator 上——这些方法本质是
# "市场派生量化指标"，归类于 market 维度
def _bind_market_misc_extensions() -> None:
    """运行时把 4 个方法挂载到 MarketMiscAggregator 类。

    用 monkey-patch 而非直接修改 class body 是为了让 4 个方法定义独立成块
    便于未来按职责进一步拆分（与早期 12 个方法是同样的归类）。
    """

