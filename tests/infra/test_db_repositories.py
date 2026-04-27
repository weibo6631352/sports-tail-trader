from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.infra.db.models import MarketModel
from polymarket_trader.infra.db.repositories import (
    MarketRepository,
    _market_matches_snapshot_filters,
    _sort_market_snapshots,
)
from tests.helpers.markets import build_binary_market


def _market(
    *,
    condition_id: str,
    market_slug: str,
    fee_rate_bps: int | None = None,
    taker_base_fee_bps: int | None = None,
    maker_base_fee_bps: int | None = None,
    fee_rate_updated_at: datetime | None = None,
) -> Market:
    return build_binary_market(
        condition_id=condition_id,
        market_slug=market_slug,
        no_token_id=f"no-{condition_id}",
        yes_token_id=f"yes-{condition_id}",
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        trading_status=TradingStatus.ELIGIBLE,
        fee_rate_bps=fee_rate_bps,
        taker_base_fee_bps=taker_base_fee_bps,
        maker_base_fee_bps=maker_base_fee_bps,
        fee_rate_updated_at=fee_rate_updated_at,
    )


def test_market_snapshot_helpers_use_normalized_fee_values() -> None:
    normalized = MarketModel.from_domain(
        _market(
            condition_id="condition-1",
            market_slug="sample-market-a",
            fee_rate_bps=1000,
            taker_base_fee_bps=1000,
            fee_rate_updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        trace_id="trace-1",
        source="test",
        raw_payload={"feeSchedule": {"rate": "0.072"}},
    ).to_domain()
    higher = _market(
        condition_id="condition-2",
        market_slug="sample-market-b",
        fee_rate_bps=200,
        taker_base_fee_bps=200,
        fee_rate_updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    missing = _market(
        condition_id="condition-3",
        market_slug="sample-market-c",
        fee_rate_bps=None,
        taker_base_fee_bps=None,
    )

    assert normalized.fee_rate_bps == 72
    assert normalized.taker_base_fee_bps == 72
    assert _market_matches_snapshot_filters(
        normalized,
        fee_rate_bps_max=100,
        taker_base_fee_bps_max=100,
    )
    assert not _market_matches_snapshot_filters(normalized, fee_rate_bps_min=100)

    sorted_markets = _sort_market_snapshots(
        (missing, normalized, higher),
        sort_by="fee_rate_bps",
        sort_direction="desc",
    )
    assert tuple(market.market_slug for market in sorted_markets) == (
        "sample-market-b",
        "sample-market-a",
        "sample-market-c",
    )


def test_market_repository_save_markets_updates_fee_columns() -> None:
    class _CapturingRepository(MarketRepository):
        def __init__(self) -> None:
            super().__init__(session=None)  # type: ignore[arg-type]
            self.calls: list[dict[str, object]] = []

        async def _bulk_upsert(
            self,
            model,
            rows,
            *,
            conflict_columns,
            update_columns,
        ) -> int:
            self.calls.append(
                {
                    "model": model,
                    "rows": rows,
                    "conflict_columns": tuple(conflict_columns),
                    "update_columns": tuple(update_columns),
                }
            )
            return len(rows)

    async def run() -> None:
        repository = _CapturingRepository()
        market = _market(
            condition_id="condition-1",
            market_slug="sample-market-a",
            fee_rate_bps=72,
            taker_base_fee_bps=72,
            maker_base_fee_bps=0,
            fee_rate_updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ).with_fee_schedule(
            fees_enabled=True,
            maker_base_fee_bps=0,
            taker_base_fee_bps=72,
        )

        await repository.save_markets([market], trace_id="trace-1", source="test")

        call = repository.calls[0]
        update_columns = call["update_columns"]
        assert isinstance(update_columns, tuple)
        assert "fees_enabled" in update_columns
        assert "maker_base_fee_bps" in update_columns
        assert "taker_base_fee_bps" in update_columns
        assert "fee_rate_bps" in update_columns
        assert "fee_rate_updated_at" in update_columns

    asyncio.run(run())
