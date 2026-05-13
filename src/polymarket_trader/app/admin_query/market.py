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


__all__ = ["AdminMarketQueryMixin"]
