from __future__ import annotations

from typing import Any, Iterable, Sequence, cast

from sqlalchemy import select

from polymarket_trader.domain.market import Market
from polymarket_trader.infra.db.models import MarketModel
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
    _limit_offset,
    _row_dict,
)


def _market_matches_snapshot_filters(
    market: Market,
    *,
    fee_rate_bps_min: int | None = None,
    fee_rate_bps_max: int | None = None,
    maker_base_fee_bps_min: int | None = None,
    maker_base_fee_bps_max: int | None = None,
    taker_base_fee_bps_min: int | None = None,
    taker_base_fee_bps_max: int | None = None,
) -> bool:
    if fee_rate_bps_min is not None and (market.fee_rate_bps is None or market.fee_rate_bps < fee_rate_bps_min):
        return False
    if fee_rate_bps_max is not None and (market.fee_rate_bps is None or market.fee_rate_bps > fee_rate_bps_max):
        return False
    if maker_base_fee_bps_min is not None and (
        market.maker_base_fee_bps is None or market.maker_base_fee_bps < maker_base_fee_bps_min
    ):
        return False
    if maker_base_fee_bps_max is not None and (
        market.maker_base_fee_bps is None or market.maker_base_fee_bps > maker_base_fee_bps_max
    ):
        return False
    if taker_base_fee_bps_min is not None and (
        market.taker_base_fee_bps is None or market.taker_base_fee_bps < taker_base_fee_bps_min
    ):
        return False
    if taker_base_fee_bps_max is not None and (
        market.taker_base_fee_bps is None or market.taker_base_fee_bps > taker_base_fee_bps_max
    ):
        return False
    return True


def _market_snapshot_sort_value(market: Market, sort_by: str) -> object | None:
    return {
        "market_slug": market.market_slug,
        "fee_rate_bps": market.fee_rate_bps,
        "fee_rate_updated_at": market.fee_rate_updated_at,
        "maker_base_fee_bps": market.maker_base_fee_bps,
        "taker_base_fee_bps": market.taker_base_fee_bps,
    }[sort_by]


def _sort_market_snapshots(
    markets: Sequence[Market],
    *,
    sort_by: str | None = None,
    sort_direction: str = "desc",
) -> tuple[Market, ...]:
    if sort_by is None:
        return tuple(markets)
    supported = {
        "market_slug",
        "fee_rate_bps",
        "fee_rate_updated_at",
        "maker_base_fee_bps",
        "taker_base_fee_bps",
    }
    if sort_by not in supported:
        return tuple(markets)
    if sort_by == "market_slug":
        return tuple(sorted(markets, key=lambda market: market.market_slug, reverse=sort_direction == "desc"))
    present = [market for market in markets if _market_snapshot_sort_value(market, sort_by) is not None]
    missing = [market for market in markets if _market_snapshot_sort_value(market, sort_by) is None]
    present.sort(key=lambda market: market.market_slug)
    present.sort(
        key=lambda market: cast(Any, _market_snapshot_sort_value(market, sort_by)),
        reverse=sort_direction == "desc",
    )
    return tuple(present + missing)


class MarketRepository(BaseRepository):
    """市场快照仓储。"""

    async def save_market(
        self,
        market: Market,
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> Market:
        await self.save_markets([market], trace_id=trace_id, source=source, raw_payloads=[raw_payload])
        return market

    async def save_markets(
        self,
        markets: Iterable[Market],
        *,
        trace_id: str | None = None,
        source: str | None = None,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        markets = tuple(markets)
        payloads = raw_payloads or (None,) * len(markets)
        rows = [
            _row_dict(
                MarketModel.from_domain(
                    market,
                    trace_id=trace_id,
                    source=source,
                    raw_payload=payload,
                )
            )
            for market, payload in zip(markets, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            MarketModel,
            rows,
            conflict_columns=("condition_id",),
            update_columns=(
                "trace_id",
                "source",
                "market_slug",
                "token_ids",
                "outcomes",
                "market_name",
                "market_question",
                "event_id",
                "event_title",
                "event_slug",
                "tick_size",
                "min_order_size",
                "neg_risk",
                "fees_enabled",
                "maker_base_fee_bps",
                "taker_base_fee_bps",
                "fee_rate_bps",
                "fee_rate_updated_at",
                "category",
                "tags",
                "matched_keywords",
                "trading_status",
                "reject_reason",
                "raw_payload",
                "updated_at",
            ),
        )

    async def get_by_condition_id(self, condition_id: str) -> Market | None:
        row = await self._session.scalar(select(MarketModel).where(MarketModel.condition_id == condition_id))
        return None if row is None else row.to_domain()

    async def get_by_market_slug(self, market_slug: str) -> Market | None:
        row = await self._session.scalar(select(MarketModel).where(MarketModel.market_slug == market_slug))
        return None if row is None else row.to_domain()

    async def get_by_token_id(self, token_id: str) -> Market | None:
        row = await self._session.scalar(
            select(MarketModel).where(MarketModel.token_ids.contains([token_id]))
        )
        return None if row is None else row.to_domain()

    async def list_by_condition_ids(self, condition_ids: Sequence[str]) -> tuple[Market, ...]:
        """按 ``condition_id`` 集合批量取 markets——给跨表聚合（如 PnL breakdown）用。"""

        ids = tuple(set(cid for cid in condition_ids if cid))
        if not ids:
            return ()
        stmt = select(MarketModel).where(MarketModel.condition_id.in_(ids))
        result = await self._session.scalars(stmt)
        return tuple(row.to_domain() for row in result.all())

    async def list_markets_snapshot(
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
        sort_by: str | None = None,
        sort_direction: str = "desc",
    ) -> RepositoryPage[Market]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(MarketModel)
        if trading_status is not None:
            stmt = stmt.where(MarketModel.trading_status == trading_status)
        if fees_enabled is not None:
            stmt = stmt.where(MarketModel.fees_enabled.is_(fees_enabled))

        needs_domain_fee_view = any(
            value is not None
            for value in (
                fee_rate_bps_min,
                fee_rate_bps_max,
                maker_base_fee_bps_min,
                maker_base_fee_bps_max,
                taker_base_fee_bps_min,
                taker_base_fee_bps_max,
            )
        ) or sort_by in {
            "fee_rate_bps",
            "fee_rate_updated_at",
            "maker_base_fee_bps",
            "taker_base_fee_bps",
        }
        if needs_domain_fee_view:
            stmt = stmt.order_by(MarketModel.updated_at.desc(), MarketModel.id.desc())
            rows = list((await self._session.scalars(stmt)).all())
            markets = tuple(row.to_domain() for row in rows)
            filtered = tuple(
                market
                for market in markets
                if _market_matches_snapshot_filters(
                    market,
                    fee_rate_bps_min=fee_rate_bps_min,
                    fee_rate_bps_max=fee_rate_bps_max,
                    maker_base_fee_bps_min=maker_base_fee_bps_min,
                    maker_base_fee_bps_max=maker_base_fee_bps_max,
                    taker_base_fee_bps_min=taker_base_fee_bps_min,
                    taker_base_fee_bps_max=taker_base_fee_bps_max,
                )
            )
            sorted_items = _sort_market_snapshots(
                filtered,
                sort_by=sort_by,
                sort_direction=sort_direction,
            )
            page_items = sorted_items[offset : offset + limit]
            return RepositoryPage(
                items=tuple(page_items),
                total=len(filtered),
                limit=limit,
                offset=offset,
            )

        if fee_rate_bps_min is not None:
            stmt = stmt.where(MarketModel.fee_rate_bps >= fee_rate_bps_min)
        if fee_rate_bps_max is not None:
            stmt = stmt.where(MarketModel.fee_rate_bps <= fee_rate_bps_max)
        if maker_base_fee_bps_min is not None:
            stmt = stmt.where(MarketModel.maker_base_fee_bps >= maker_base_fee_bps_min)
        if maker_base_fee_bps_max is not None:
            stmt = stmt.where(MarketModel.maker_base_fee_bps <= maker_base_fee_bps_max)
        if taker_base_fee_bps_min is not None:
            stmt = stmt.where(MarketModel.taker_base_fee_bps >= taker_base_fee_bps_min)
        if taker_base_fee_bps_max is not None:
            stmt = stmt.where(MarketModel.taker_base_fee_bps <= taker_base_fee_bps_max)

        sort_column = {
            "market_slug": MarketModel.market_slug,
            "fee_rate_bps": MarketModel.fee_rate_bps,
            "fee_rate_updated_at": MarketModel.fee_rate_updated_at,
            "maker_base_fee_bps": MarketModel.maker_base_fee_bps,
            "taker_base_fee_bps": MarketModel.taker_base_fee_bps,
        }.get(sort_by or "")
        if sort_column is None:
            stmt = stmt.order_by(MarketModel.updated_at.desc(), MarketModel.id.desc())
        elif sort_direction == "asc":
            stmt = stmt.order_by(sort_column.asc().nullslast(), MarketModel.market_slug.asc(), MarketModel.id.desc())
        else:
            stmt = stmt.order_by(sort_column.desc().nullslast(), MarketModel.market_slug.asc(), MarketModel.id.desc())
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)


__all__ = ["MarketRepository"]
