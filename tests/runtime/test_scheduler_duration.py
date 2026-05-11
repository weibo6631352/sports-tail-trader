"""Scheduler 必须直接记录每次运行真实耗时（N9）。

旧实现没有 last_duration_ms，外部观测者只能从 last_finished_at - last_started_at
算 duration——当 start 字段被下一轮覆盖时差值可能是负数（实测 last_dur=-500.9ms /
-5000.8ms）。修复后 scheduler 在 _run_once 完成时直接计算 finished - started 写入
last_duration_ms，外部读这个字段即可。
"""

from __future__ import annotations

import asyncio

from polymarket_trader.runtime.scheduler import Scheduler


def test_scheduler_records_last_duration_ms_per_run() -> None:
    async def run() -> tuple[float | None, int]:
        scheduler = Scheduler()
        call_count = 0

        async def job() -> None:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.005)

        scheduler.register_job(
            "test_job",
            job,
            priority="P3",
            interval_seconds=1.0,
            start=True,
            run_immediately=True,
        )
        # 给调度器一点时间完成至少一次运行。
        for _ in range(40):
            await asyncio.sleep(0.01)
            snapshot = scheduler.snapshot()
            duration = snapshot.jobs[0].last_duration_ms
            if duration is not None:
                break
        await scheduler.shutdown()
        return scheduler.snapshot().jobs[0].last_duration_ms, call_count

    last_duration_ms, call_count = asyncio.run(run())

    assert call_count >= 1
    # 真实耗时必须为正——这是 N9 的核心断言：外部不再用 finished-started 自己算。
    assert last_duration_ms is not None
    assert last_duration_ms >= 0.0
