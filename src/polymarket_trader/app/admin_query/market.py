"""市场维度的只读查询：列表、单条、盘口、midpoint、价格历史、盘口快照历史、流动性、冲击成本。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.infra.db.repositories.market import sort_markets
from polymarket_trader.infra.polymarket import PolymarketClientError
from polymarket_trader.app.admin_serialization import decimal_text, page_payload
from polymarket_trader.app.admin_service_helpers import (
    MarketFeeSortField,
    SortDirection,
    _RepositoryGroup,
    _market_matches_fee_filters,
    _orderbook_has_no_quotes,
)


def _serialize_side_summary(side: Any) -> dict[str, Any]:
    """DerivedMetrics.bid_side / ask_side → 兼容旧 admin /orderbook-depth 输出格式。"""
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
    base = {
        "window_s": w.window_s,
        "available": w.available,
    }
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
    """DerivedMetrics 拍平成 /orderbook-depth 的 ``liquidity_metrics`` payload。"""
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
        # 风向信号 (publisher 嵌入 delta_store.direction_signal 投影):
        # None 表示 sample 不足 / token 首次推送, admin 可独立调
        # /markets/orderbook-direction 看 raw deltas.
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
    """从 Market 元数据提取盘口分类(不依赖策略层).

    主要字段:
    - sports_market_type: gamma 原始字段(moneyline/totals/spreads/...)最权威
    - inferred_kind: 从 market_slug 启发式推断(若 gamma 字段缺失)
    - market_question: 完整问题文本(给 UI 直接展示)
    - event_slug / market_slug: 上下文
    """
    if market is None:
        return {"available": False, "reason": "market_not_found_in_registry"}
    smt = getattr(market, "sports_market_type", None)
    slug = (getattr(market, "market_slug", "") or "").lower()
    question = getattr(market, "market_question", None)
    # 启发式推断:仅在 gamma sports_market_type 缺失时使用,且关键字更严避免误报.
    # slug 通用含 "-",所以不用 "-" 判断;只用强标志词.
    inferred = None
    if not smt:
        if "first-blood" in slug or "first-touchdown" in slug or "first-goal" in slug or "first-set" in slug:
            inferred = "first_event_prop"
        elif "spread-" in slug or "handicap" in slug:
            inferred = "spreads_candidate"
        elif "over-" in slug or "under-" in slug or "-totals" in slug or "points-scored" in slug or "total-goals" in slug:
            inferred = "totals_candidate"
        elif "1h-" in slug or "2h-" in slug or "first-half" in slug or "second-half" in slug \
                or "q1-" in slug or "q2-" in slug or "q3-" in slug or "q4-" in slug \
                or "first-inning" in slug or "first-set" in slug or "second-set" in slug:
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
    """OrderbookDirectionSignal → dict (raw deltas + microprice + 真实价区 + velocity + 复合)."""
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


class AdminMarketQueryMixin:
    """市场维度的 admin 只读查询。"""

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
        if registry.markets or not self._has_db_session_factory():
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
            page = self._slice_sequence(markets, limit=limit, offset=offset)
            items = [
                self._serializer().market_view(
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

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
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

        page = await self._with_repositories(_query)
        registry = self._registry_snapshot()
        account = self._account_snapshot()
        items = [
            self._serializer().market_view(
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

    async def get_market(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any] | None:
        market = self._resolve_market(
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
        )
        if market is None and self._has_db_session_factory():
            async def _query(repos: _RepositoryGroup) -> Market | None:
                if condition_id is not None:
                    market_by_condition = await repos.market.get_by_condition_id(condition_id)
                    if market_by_condition is not None:
                        return market_by_condition
                if token_id is not None:
                    market_by_token = await repos.market.get_by_token_id(token_id)
                    if market_by_token is not None:
                        return market_by_token
                if market_slug is not None:
                    return await repos.market.get_by_market_slug(market_slug)
                return None

            market = await self._with_repositories(_query)
        if market is None:
            return None
        return self._serializer().market_view(market)

    async def get_market_orderbook(
        self,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any] | None:
        market = self._resolve_market(
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
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
        return self._serializer().market_orderbook(
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
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
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
        return self._serializer().market_midpoint(
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
        return self._serializer().market_prices_history(
            token_id=token_id,
            history=history,
            interval=interval,
            fidelity=fidelity,
        )

    async def get_orderbook_direction(
        self,
        *,
        token_id: str,
        window_seconds: float = 10.0,
    ) -> dict[str, Any]:
        """读 OrderbookDeltaStore 多时点 delta 信号 + 落审计。

        score [-1,+1] (基于 mid 价位移归一);direction_label='yes'/'no'/'neutral';
        raw bid/ask price/size delta;confidence 反映 sample 数和数据完整性。
        每次查询都写 ORDERBOOK_DIRECTION_QUERIED 审计,事后可复盘"为什么这一刻判断方向"。
        """

        from uuid import uuid4

        from polymarket_trader.domain.events import DomainEvent, DomainEventType
        from polymarket_trader.domain.events import OutboxPriority

        store = self.runtime.orderbook_delta_store if self.runtime else None
        if store is None:
            payload: dict[str, Any] = {
                "token_id": token_id,
                "window_seconds": window_seconds,
                "signal": None,
                "reason": "store_unavailable",
            }
            return payload

        signal = store.direction_signal(token_id, window_seconds=window_seconds)
        if signal is None:
            payload = {
                "token_id": token_id,
                "window_seconds": window_seconds,
                "signal": None,
                "reason": "insufficient_samples",
                "tracked_tokens": len(store.tracked_tokens()),
            }
        else:
            payload = {"token_id": token_id, **_serialize_direction_signal(signal)}

        # 异步落 audit。失败不阻塞查询 (§7 主链路不被反向阻塞)。
        if self.runtime is not None and getattr(self.runtime, "event_bus", None) is not None:
            try:
                event = DomainEvent(
                    trace_id=uuid4().hex,
                    event_type=DomainEventType.ORDERBOOK_DIRECTION_QUERIED,
                    event_id=uuid4().hex,
                    reason="admin_query",
                    payload=dict(payload),
                )
                self.runtime.event_bus.publish_nowait(OutboxPriority.P3, event)
            except Exception:
                pass
        return payload

    async def get_orderbook_direction_multi(
        self,
        *,
        token_id: str,
        windows: tuple[float, ...],
    ) -> dict[str, Any]:
        """同一 token 同一快照下多窗口对比版本(2/5/10/30s 等).

        一次查询返回每个窗口的完整 signal,审计只落一条 ORDERBOOK_DIRECTION_QUERIED
        (windows 列表写入 payload).typical 用法:策略侧 micro-window(2/5s) 看抢单
        瞬态 + macro-window(30/60s) 看趋势,综合判断进场时机.
        """
        from uuid import uuid4
        from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority

        store = self.runtime.orderbook_delta_store if self.runtime else None
        if store is None:
            return {"token_id": token_id, "windows": list(windows), "signals": [],
                    "reason": "store_unavailable"}

        signals_payload: list[dict[str, Any]] = []
        for w in windows:
            signal = store.direction_signal(token_id, window_seconds=w)
            if signal is None:
                signals_payload.append({"window_seconds": w, "signal": None,
                                        "reason": "insufficient_samples"})
            else:
                signals_payload.append(_serialize_direction_signal(signal))
        payload: dict[str, Any] = {
            "token_id": token_id,
            "windows_requested": list(windows),
            "tracked_tokens": len(store.tracked_tokens()),
            "signals": signals_payload,
        }
        if self.runtime is not None and getattr(self.runtime, "event_bus", None) is not None:
            try:
                event = DomainEvent(
                    trace_id=uuid4().hex,
                    event_type=DomainEventType.ORDERBOOK_DIRECTION_QUERIED,
                    event_id=uuid4().hex,
                    reason="admin_query_multi",
                    payload=dict(payload),
                )
                self.runtime.event_bus.publish_nowait(OutboxPriority.P3, event)
            except Exception:
                pass
        return payload

    async def list_orderbook_history(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        token_id: str | None = None,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """历史盘口快照查询，按 ``received_at`` 倒序。

        ``orderbook_snapshots`` 表已经在落，本接口只暴露 GET。复盘"入场那一秒
        的盘口"用，按 token_id / condition_id + 时间窗过滤。
        """

        if not self._has_db_session_factory():
            page: RepositoryPage[Any] = RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
            return page_payload(page, serializer=self._serializer().orderbook)

        async def _query(repos: _RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.orderbook.list_snapshots(
                limit=limit,
                offset=offset,
                token_id=token_id,
                condition_id=condition_id,
                time_range=time_range,
            )

        page = await self._with_repositories(_query)
        return page_payload(page, serializer=self._serializer().orderbook)


    def get_market_liquidity(
        self,
        *,
        token_id: str,
        condition_id: str | None = None,
        market_slug: str | None = None,
        depth_ticks: int = 5,
    ) -> dict[str, Any] | None:
        """盘口流动性快照（纯内存，热 WS 数据，零 DB，零 P0 影响）。

        返回：
        - vwap_mid: 成交量加权中间价（bid/ask 各侧前 depth_ticks 档）
        - effective_spread: 有效买卖价差
        - snapshot_age_ms: 盘口快照距现在的延迟（毫秒）
        - bid/ask 深度分档（按累计 USDC 分 1/5/10 档）
        """

        snapshot = self._market_ws_snapshot(token_id)
        if snapshot is None or _orderbook_has_no_quotes(snapshot):
            return None

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        received_ms = int(snapshot.received_at.timestamp() * 1000)
        age_ms = now_ms - received_ms

        # WS 快照档位无序且含 0-size 占位，必须先过滤再排序才能计算有效深度。
        active_bids = sorted(
            (lv for lv in snapshot.bids if lv.size > Decimal("0")),
            key=lambda lv: lv.price,
            reverse=True,  # 最优 bid（价格最高）在前
        )
        active_asks = sorted(
            (lv for lv in snapshot.asks if lv.size > Decimal("0")),
            key=lambda lv: lv.price,
            reverse=False,  # 最优 ask（价格最低）在前
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

        # 成交量加权中间价：取排序后前 N 档（最优档开始）
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
        """下单前冲击成本估算（纯内存，热 WS 盘口，零 DB，零 P0 影响）。

        给定目标买入 USDC，逐档遍历 ask 侧，估算：
        - estimated_avg_price: 预计成交均价
        - estimated_shares: 预计成交份额
        - price_impact_bps: 相对 best_ask 的价格冲击（bps）
        - fillable_usdc: 当前深度能填满的 USDC
        - unfillable_usdc: 深度不足无法填满的 USDC
        """

        snapshot = self._market_ws_snapshot(token_id)
        if snapshot is None or _orderbook_has_no_quotes(snapshot):
            return None

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        received_ms = int(snapshot.received_at.timestamp() * 1000)
        age_ms = now_ms - received_ms

        # 从最优 ask（价格最低）开始逐档吃单，必须排序并过滤 0-size 占位档。
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


    def orderbook_depth_snapshot(
        self,
        *,
        token_id: str,
        windows_s: tuple[float, ...] = (2.0, 3.0, 5.0, 10.0),
    ) -> dict[str, Any]:
        """完整盘口资金分布 + 我方 resting/持仓 + 多窗口波动。

        100% 读 ``OrderbookDerivedStore`` 的预计算缓存 (由 market_ws push 时
        ``OrderbookDerivedPublisher`` 异步刷新). 无现场重算, 不阻塞 P0 event loop.

        cache 未填充 (启动早期或对应 token 还没推送) → ``available=False,
        reason="derived_not_ready"``. 几秒内会被填上.

        ``windows_s`` 参数兼容旧契约: publisher 默认按 ``DEFAULT_WINDOWS_SECONDS``
        预算; 若调用方需要别的窗口长度, 应升级 publisher 而不是 admin 现场算.
        """
        if self.runtime is None or self.runtime.market_ws_worker is None:
            return {"token_id": token_id, "available": False, "reason": "market_ws_unavailable"}
        ws = self.runtime.market_ws_worker
        current = ws.snapshot(token_id)
        if current is None:
            return {"token_id": token_id, "available": False, "reason": "snapshot_not_found"}

        derived_store = getattr(self.runtime, "orderbook_derived_store", None)
        derived = derived_store.get(token_id) if derived_store is not None else None
        if derived is None:
            return {"token_id": token_id, "available": False, "reason": "derived_not_ready"}

        # market 分类: 跟随 market_registry 元数据 (非盘口派生)
        market_obj = (
            self.runtime.registry.get_by_token_id(token_id)
            if self.runtime.registry else None
        )
        classification = _classify_market(market_obj)

        # 我方持仓 + resting orders: 来自 account_state / paper_ledger, 不属于盘口派生
        our_position_shares = Decimal("0")
        our_position_cost = Decimal("0")
        our_resting_buy_size = Decimal("0")
        our_resting_buy_usdc = Decimal("0")
        our_resting_sell_size = Decimal("0")
        our_resting_sell_usdc = Decimal("0")
        if self.runtime.paper_ledger is not None:
            shares = self.runtime.paper_ledger.positions.get(token_id, Decimal("0"))
            our_position_shares = shares
            our_position_cost = self.runtime.paper_ledger.cost_basis_usdc.get(token_id, Decimal("0"))
        account = self._account_snapshot() if self.runtime.account_state_store else None
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

        history = self.runtime.orderbook_history_buffer if self.runtime else None
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

    def liquidity_summary_snapshot(self, *, top_n: int = 20) -> dict[str, Any]:
        """全市场盘口总金额聚合 + top N 深度市场.

        扫所有 ws._states.snapshot,聚合:
        - total_bid_usdc / total_ask_usdc / total_usdc (双边)
        - whale_total_usdc (USDC ≥ $50 的大单总和)
        - by_classification: 按 sports_market_type 分组
        - top_n_deepest: 单市场 total_usdc 最高的 N 个市场
        """
        if self.runtime is None or self.runtime.market_ws_worker is None:
            return {"available": False, "reason": "market_ws_unavailable"}
        from decimal import Decimal as D
        WHALE = D("50")
        ws = self.runtime.market_ws_worker
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
            if snap is None: continue
            tracked_tokens += 1
            mbid = D("0"); mask = D("0"); mw_bc = mw_ac = 0
            mw_bu = D("0"); mw_au = D("0")
            for lvl in snap.bids:
                u = lvl.size * lvl.price
                mbid += u
                if u >= WHALE: mw_bc += 1; mw_bu += u
            for lvl in snap.asks:
                u = lvl.size * lvl.price
                mask += u
                if u >= WHALE: mw_ac += 1; mw_au += u
            total_bid_usdc += mbid; total_ask_usdc += mask
            whale_bid_usdc += mw_bu; whale_ask_usdc += mw_au
            whale_bid_count += mw_bc; whale_ask_count += mw_ac
            # 按 classification 分组
            market_obj = self.runtime.registry.get_by_token_id(token_id) if self.runtime.registry else None
            cls = _classify_market(market_obj)
            kind = cls.get("sports_market_type") or cls.get("inferred_kind") or "unknown"
            agg = by_classification.setdefault(kind, {"markets": 0, "bid_usdc": D("0"), "ask_usdc": D("0")})
            agg["markets"] += 1
            agg["bid_usdc"] += mbid; agg["ask_usdc"] += mask
            # top N 深度市场
            market_depths.append({
                "token_id": token_id[:24] + "...",
                "market_slug": snap.market_slug,
                "total_usdc": str(mbid + mask),
                "bid_usdc": str(mbid),
                "ask_usdc": str(mask),
            })
        # 每市场加双边资金比例(bid_share = bid/(bid+ask))
        for row in market_depths:
            b = D(row["bid_usdc"]); a = D(row["ask_usdc"])
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
        # 序列化 by_classification 的 Decimal + 比例
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

    def event_bundle_snapshot(self, *, event_slug: str) -> dict[str, Any]:
        """一个 event(比赛)下所有 market(condition)的聚合视图.

        一次拿到 ML / Totals / Spreads / 分节 prop 全部市场,带 classification +
        盘口 best bid/ask + total_usdc.前端单页展示一场比赛全盘口必备.
        """
        if self.runtime is None or self.runtime.registry is None:
            return {"available": False, "reason": "registry_unavailable"}
        from decimal import Decimal as D
        registry = self.runtime.registry.snapshot()
        matched = [m for m in registry.markets if (m.event_slug or "") == event_slug]
        if not matched:
            return {"available": False, "reason": "event_not_found", "event_slug": event_slug}
        ws = self.runtime.market_ws_worker
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
            # vig / overround:Polymarket 二元市场,正常 sum(best_ask) = 1.
            # 实际 sum > 1 表示有 vig(MM 抽成);sum < 1 表示套利机会(罕见).
            asks_for_vig: list[D] = []
            for ti in tokens_info:
                if ti.get("best_ask"):
                    try: asks_for_vig.append(D(ti["best_ask"]))
                    except Exception: pass
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

    def market_data_health_snapshot(
        self,
        *,
        condition_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any]:
        """单市场所有数据源连接状态 + 新鲜度。

        覆盖: market_ws / live_state / inplay / pregame / livescore. 用于回答
        "这个市场现在能不能交易,各路数据是否新鲜"。
        """
        from datetime import datetime, timezone
        if self.runtime is None:
            return {"available": False, "reason": "runtime_unavailable"}
        registry = self._registry_snapshot()
        # 解析 market
        market = None
        if condition_id is not None:
            market = next((m for m in registry.markets if m.condition_id == condition_id), None)
        elif token_id is not None:
            for m in registry.markets:
                if token_id in (m.token_ids or ()):
                    market = m
                    break
        if market is None:
            return {"available": False, "reason": "market_not_found",
                    "condition_id": condition_id, "token_id": token_id}

        now = datetime.now(timezone.utc)
        result: dict[str, Any] = {
            "available": True,
            "condition_id": market.condition_id,
            "market_slug": market.market_slug,
            "event_slug": market.event_slug,
            "classification": _classify_market(market),
        }
        # market_ws
        ws_info: dict[str, Any] = {}
        if self.runtime.market_ws_worker is not None:
            ws = self.runtime.market_ws_worker
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

        # live_state from entry_metadata
        live_info: dict[str, Any] = {}
        meta_store = self.runtime.entry_metadata_store
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
                        "status": lg.get("status"), "period": lg.get("period"),
                        "home_score": lg.get("home_score"), "away_score": lg.get("away_score"),
                        "seconds_remaining": lg.get("seconds_remaining"),
                        "sport": lg.get("sport"),
                    }
        result["live_state"] = live_info

        # inplay/livescore per-sport status:从 SportsLiveAggregateClient 拿
        # source_detail_status() 返回所有数据源 per-sport 快照;按 sport 过滤
        inplay_info: list[dict[str, Any]] = []
        sport = (live_info.get("state_summary") or {}).get("sport") if isinstance(live_info, dict) else None
        if self.runtime.sports_live_state_client is not None:
            try:
                all_status = self.runtime.sports_live_state_client.source_detail_status()
                if sport:
                    inplay_info = [s for s in all_status if s.get("sport") == sport]
                else:
                    inplay_info = all_status
            except Exception as exc:
                inplay_info = [{"error": str(exc)[:120]}]
        result["sources_per_sport"] = inplay_info

        # pregame worker status
        pg_worker = getattr(self.runtime, "pregame_worker", None)
        if pg_worker is not None and hasattr(pg_worker, "status_snapshot"):
            result["pregame"] = dict(pg_worker.status_snapshot())
        else:
            result["pregame"] = {"enabled": False}

        return result


__all__ = ["AdminMarketQueryMixin"]
