"""验证 fire-and-forget lifecycle 发布任务被强引用追踪。

Python asyncio 文档明确：未保留强引用的 ``create_task`` 任务可能在运行中被 GC
回收，导致审计 / lifecycle 事件静默丢失（CLAUDE.md §7：审计副作用必须可靠承接，
不阻塞 P0 主链路但也不能丢）。`PolymarketOrderExecutor` 通过
``_background_tasks`` 集合保持强引用，并在 done_callback 中 discard 和记录异常。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from polymarket_trader.infra.polymarket.order_executor import (
    PolymarketOrderExecutor,
)


@pytest.fixture()
def executor() -> PolymarketOrderExecutor:
    inst = PolymarketOrderExecutor(client=None, outbox=None)
    yield inst
    inst.close()


def test_spawn_background_tracks_task_until_done(executor: PolymarketOrderExecutor) -> None:
    async def run() -> None:
        gate = asyncio.Event()

        async def waiter() -> None:
            await gate.wait()

        task = executor._spawn_background(waiter(), name="test.waiter")
        assert task in executor._background_tasks
        gate.set()
        await asyncio.sleep(0)
        await task
        # done_callback 在事件循环下一轮再 discard
        await asyncio.sleep(0)
        assert task not in executor._background_tasks

    asyncio.run(run())


def test_spawn_background_logs_exception(
    executor: PolymarketOrderExecutor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        async def boom() -> None:
            raise RuntimeError("audit-pub-failed")

        with caplog.at_level(logging.WARNING, logger="polymarket_trader.infra.polymarket.order_executor"):
            task = executor._spawn_background(boom(), name="test.boom")
            # 等任务完成 + done_callback 触发
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        messages = [rec.getMessage() for rec in caplog.records]
        assert any("background_task_failed" in m and "test.boom" in m for m in messages)
        assert task not in executor._background_tasks

    asyncio.run(run())


def test_spawn_background_suppresses_cancelled_error(
    executor: PolymarketOrderExecutor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        async def loop_forever() -> None:
            await asyncio.sleep(3600)

        with caplog.at_level(logging.WARNING, logger="polymarket_trader.infra.polymarket.order_executor"):
            task = executor._spawn_background(loop_forever(), name="test.cancel")
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        # 取消是正常关闭信号，不应产生 background_task_failed 警告
        warnings = [rec for rec in caplog.records if "background_task_failed" in rec.getMessage()]
        assert warnings == []
        assert task not in executor._background_tasks

    asyncio.run(run())


def test_aclose_drains_background_tasks() -> None:
    async def run() -> None:
        inst = PolymarketOrderExecutor(client=None, outbox=None)
        finished: list[str] = []

        async def slow() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                finished.append("cancelled")
                raise

        inst._spawn_background(slow(), name="test.drain")
        await asyncio.sleep(0)
        assert len(inst._background_tasks) == 1
        await inst.aclose()
        assert inst._background_tasks == set()
        assert finished == ["cancelled"]

    asyncio.run(run())
