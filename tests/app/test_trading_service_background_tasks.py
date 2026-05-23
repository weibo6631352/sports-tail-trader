"""验证 TradingService 风控拒绝降级路径的 fire-and-forget 任务被强引用追踪。

CLAUDE.md §7：审计副作用（含 risk_rejected）必须可靠承接；create_task 默认仅
被事件循环弱引用，需要显式强引用集合防止 GC 中途吞掉任务。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from polymarket_trader.app.trading_service import TradingService


def test_spawn_background_tracks_task() -> None:
    async def run() -> None:
        svc = TradingService()
        gate = asyncio.Event()

        async def waiter() -> None:
            await gate.wait()

        task = svc._spawn_background(waiter(), name="test.waiter")
        assert task in svc._background_tasks
        gate.set()
        await task
        await asyncio.sleep(0)
        assert task not in svc._background_tasks
        await svc.aclose()

    asyncio.run(run())


def test_spawn_background_logs_exception(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        svc = TradingService()

        async def boom() -> None:
            raise RuntimeError("risk-pub-failed")

        with caplog.at_level(logging.WARNING, logger="polymarket_trader.app.trading_service"):
            task = svc._spawn_background(boom(), name="test.boom")
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        messages = [rec.getMessage() for rec in caplog.records]
        assert any("background_task_failed" in m and "test.boom" in m for m in messages)
        assert task not in svc._background_tasks
        await svc.aclose()

    asyncio.run(run())


def test_spawn_background_suppresses_cancelled(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        svc = TradingService()

        async def loop_forever() -> None:
            await asyncio.sleep(3600)

        with caplog.at_level(logging.WARNING, logger="polymarket_trader.app.trading_service"):
            task = svc._spawn_background(loop_forever(), name="test.cancel")
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        warnings = [rec for rec in caplog.records if "background_task_failed" in rec.getMessage()]
        assert warnings == []
        assert task not in svc._background_tasks
        await svc.aclose()

    asyncio.run(run())


def test_aclose_drains_background_tasks() -> None:
    async def run() -> None:
        svc = TradingService()
        cancelled: list[str] = []

        async def slow() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append("ok")
                raise

        svc._spawn_background(slow(), name="test.drain")
        await asyncio.sleep(0)
        assert len(svc._background_tasks) == 1
        await svc.aclose()
        assert svc._background_tasks == set()
        assert cancelled == ["ok"]

    asyncio.run(run())


def test_publish_risk_rejection_fallback_tracks_task() -> None:
    """publish_nowait 缺失 → 降级到 fire-and-forget 时任务必须落入 _background_tasks。"""

    async def run() -> None:
        published: list[tuple] = []

        class FallbackBus:
            # 故意不提供 publish_nowait，触发 AttributeError 分支
            async def publish(self, priority, event) -> None:
                published.append((priority, event))

        svc = TradingService(event_bus=FallbackBus())

        # 构造最小 RiskDecision + intent 调用 _publish_risk_rejection
        from decimal import Decimal

        from polymarket_trader.domain.order import BuyOrderIntent, OrderType
        from polymarket_trader.domain.risk import RiskDecision

        intent = BuyOrderIntent(
            strategy_id="sports_tail",
            trace_id="trace-bg",
            condition_id="cond-bg",
            token_id="tok-bg",
            price=Decimal("0.5"),
            amount_usdc=Decimal("5"),
            market_slug="slug-bg",
            order_type=OrderType.GTC,
        )
        decision = RiskDecision(passed=False, reason="bg_test", failed_field=None, checks=())

        svc._publish_risk_rejection(intent=intent, risk_decision=decision, operation="entry")
        # 任务应被强引用
        assert len(svc._background_tasks) == 1
        tasks = list(svc._background_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert published and published[0][1].reason == "bg_test"
        assert svc._background_tasks == set()
        await svc.aclose()

    asyncio.run(run())
