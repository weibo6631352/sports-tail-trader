"""MarketMiscAggregator —— 市场盘口 / 流动性 / 数据健康 / 历史快照 / 派生指标。

market_detail / settlement / portfolio exposure 在各自专门 aggregator；
本 aggregator 收 market 维度其余查询（约 16 个方法）。按 CLAUDE.md
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

from polymarket_trader.api.serialization import ApiSerializer
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

    # R17 (Code Q1): RuntimeComponents 强类型 + TYPE_CHECKING 避免循环 import。
    from polymarket_trader.main import RuntimeComponents


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
        runtime: "RuntimeComponents | None",
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
        serializer: ApiSerializer | None = None,
    ) -> None:
        self._runtime = runtime
        self._session_factory = (
            session_factory
            if session_factory is not None
            else (runtime.db_session_factory if runtime is not None else None)
        )
        self._serializer = serializer or ApiSerializer.from_runtime(runtime)

    # ---------- shared helpers ----------
    def _account_snapshot(self) -> AccountSnapshot:
        if self._runtime is None:
            return AccountSnapshot()
        return self._runtime.account_state_store.snapshot()

    def _registry_snapshot(self) -> MarketRegistrySnapshot:
        if self._runtime is None:
            return MarketRegistrySnapshot(tuple())
        return self._runtime.registry.snapshot()

    def _market_ws_snapshot(self, token_id: str) -> Any | None:
        if self._runtime is None:
            return None
        return self._runtime.market_ws_worker.snapshot(token_id)

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
        if self._runtime is None:
            return None
        registry = self._runtime.registry
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
        if self._runtime is None:
            return
        bus = self._runtime.event_bus
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
    def list_markets(
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
        """市场列表——纯内存(registry snapshot),零 DB。

        启动期 registry 空时返回空 page,由 readiness gate 兜底——
        CLAUDE.md §3 "不允许任何 DB 预热,空白窗口由 reconcile_fresh 兜住"。
        """
        registry = self._registry_snapshot()
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
