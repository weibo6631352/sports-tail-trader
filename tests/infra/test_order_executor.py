from __future__ import annotations

import asyncio
from decimal import Decimal

from polymarket_trader.domain.order import BuyOrderIntent, OrderResultStatus
from polymarket_trader.infra.outbox.local_queue import LocalOutbox
from polymarket_trader.infra.polymarket.order_executor import (
    InMemoryPolymarketOrderClient,
    PolymarketOrderExecutor,
)


def test_order_executor_records_outbox_before_returning_result() -> None:
    async def run() -> None:
        outbox = LocalOutbox(max_size=32)
        executor = PolymarketOrderExecutor(
            client=InMemoryPolymarketOrderClient(),
            outbox=outbox,
        )

        result = await executor.submit(
            BuyOrderIntent(
                trace_id="trace",
                condition_id="condition",
                token_id="token",
                market_slug="sample-market-a",
                price=Decimal("0.43"),
                amount_usdc=Decimal("10"),
            )
        )

        assert result.status == OrderResultStatus.NO_FILL
        event = await outbox.get()
        assert event.event_type == "order_created"

    asyncio.run(run())
