"""``cpu_track`` decorator + SystemPerfMonitor.pipeline_cpu 行为约束。

关键不变量:
1. CpuStopwatch: 退出后 cpu_ms / wall_ms 是非负且 cpu_ms <= wall_ms.
2. @cpu_track sync: 函数执行后自动上报 (cpu_ms, wall_ms).
3. @cpu_track async: 同步 sync + 跨 await 时 cpu_ms < wall_ms (IO 等待).
4. pipeline_cpu_summary: 聚合正确 / share_pct 跨 pipeline 之和接近 100.
5. 异常吞掉: report 失败不影响业务函数.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from polymarket_trader.observability.cpu_track import (
    CpuStopwatch,
    cpu_track,
    track_async,
)
from polymarket_trader.runtime.system_perf_monitor import (
    SystemPerfMonitor,
    pipeline_cpu_summary,
)


@pytest.fixture(autouse=True)
def _reset_pipeline_samples():
    """每个测试独立: 清空 pipeline_cpu_samples 防交叉污染。"""
    monitor = SystemPerfMonitor.get()
    monitor.pipeline_cpu_samples.clear()
    yield
    monitor.pipeline_cpu_samples.clear()


# ---------- CpuStopwatch ----------


def test_cpu_stopwatch_records_cpu_and_wall() -> None:
    with CpuStopwatch() as sw:
        # 纯 CPU 工作
        s = 0
        for i in range(50000):
            s += i
    assert sw.cpu_ms >= 0
    assert sw.wall_ms >= 0
    # 纯 CPU: cpu ≈ wall (允许少量误差, cpu <= wall + epsilon)
    assert sw.cpu_ms <= sw.wall_ms + 1.0


def test_cpu_stopwatch_sleep_diverges_cpu_vs_wall() -> None:
    """time.sleep 期间 CPU 不计 → cpu << wall。"""
    with CpuStopwatch() as sw:
        time.sleep(0.05)
    assert sw.wall_ms >= 40  # 至少 40ms 墙时间
    assert sw.cpu_ms < 10    # CPU 时间几乎 0


# ---------- @cpu_track sync ----------


def test_cpu_track_sync_reports_to_monitor() -> None:
    @cpu_track("test_sync")
    def work() -> int:
        s = 0
        for i in range(10000):
            s += i
        return s

    result = work()
    assert result == sum(range(10000))

    monitor = SystemPerfMonitor.get()
    samples = monitor.pipeline_cpu_samples.get("test_sync")
    assert samples is not None
    assert len(samples) == 1
    ts, cpu_ms, wall_ms = samples[0]
    assert cpu_ms >= 0
    assert wall_ms >= 0


def test_cpu_track_sync_multiple_calls_aggregate() -> None:
    @cpu_track("test_multi")
    def work() -> None:
        for _ in range(1000):
            pass

    for _ in range(5):
        work()

    monitor = SystemPerfMonitor.get()
    samples = monitor.pipeline_cpu_samples["test_multi"]
    assert len(samples) == 5


# ---------- @cpu_track async ----------


async def test_cpu_track_async_records() -> None:
    @cpu_track("test_async")
    async def work() -> int:
        await asyncio.sleep(0.02)  # 模拟 IO await
        return 42

    result = await work()
    assert result == 42

    monitor = SystemPerfMonitor.get()
    samples = monitor.pipeline_cpu_samples["test_async"]
    assert len(samples) == 1
    ts, cpu_ms, wall_ms = samples[0]
    # IO 密集: cpu << wall (sleep 20ms 期间 CPU 不计)
    assert wall_ms >= 15
    assert cpu_ms < wall_ms / 2


async def test_track_async_adhoc_wrapping() -> None:
    """track_async() 即席包装一个 coroutine, 同样上报。"""
    async def inner() -> str:
        await asyncio.sleep(0)
        return "done"

    result = await track_async("adhoc_test", inner())
    assert result == "done"

    monitor = SystemPerfMonitor.get()
    assert "adhoc_test" in monitor.pipeline_cpu_samples


# ---------- pipeline_cpu_summary 聚合 ----------


def test_pipeline_summary_share_pct_adds_to_100() -> None:
    """跨 pipeline share_of_total_cpu_pct 之和接近 100 (允许 ±1 舍入)。"""
    monitor = SystemPerfMonitor.get()
    monitor.record_pipeline_cpu("a", cpu_ms=10.0, wall_ms=12.0)
    monitor.record_pipeline_cpu("b", cpu_ms=30.0, wall_ms=40.0)
    monitor.record_pipeline_cpu("c", cpu_ms=60.0, wall_ms=200.0)  # IO 重

    rows = pipeline_cpu_summary(monitor.pipeline_cpu_samples)
    assert len(rows) == 3
    total_share = sum(r["share_of_total_cpu_pct"] for r in rows)
    assert 99.0 <= total_share <= 101.0

    # 排序: c 最大 (60ms), b 次 (30ms), a 最小 (10ms)
    assert rows[0]["name"] == "c"
    assert rows[1]["name"] == "b"
    assert rows[2]["name"] == "a"


def test_pipeline_summary_cpu_efficiency() -> None:
    """cpu_efficiency = cpu / wall, 区分 CPU 重 vs IO 重 pipeline。"""
    monitor = SystemPerfMonitor.get()
    monitor.record_pipeline_cpu("cpu_heavy", cpu_ms=100.0, wall_ms=101.0)  # 几乎纯 CPU
    monitor.record_pipeline_cpu("io_heavy", cpu_ms=0.5, wall_ms=5000.0)    # 几乎纯 IO

    rows = pipeline_cpu_summary(monitor.pipeline_cpu_samples)
    by_name = {r["name"]: r for r in rows}
    assert by_name["cpu_heavy"]["cpu_efficiency"] > 0.95
    assert by_name["io_heavy"]["cpu_efficiency"] < 0.01


def test_pipeline_summary_respects_window() -> None:
    """window_seconds=1 只统计最近 1s 内的样本。"""
    monitor = SystemPerfMonitor.get()
    # 手动 push 一条 5s 前的样本
    monitor.pipeline_cpu_samples["old"].append((time.time() - 5.0, 100.0, 100.0))
    monitor.pipeline_cpu_samples["fresh"].append((time.time(), 10.0, 10.0))

    rows = pipeline_cpu_summary(monitor.pipeline_cpu_samples, window_seconds=2.0)
    names = {r["name"] for r in rows}
    assert "fresh" in names
    assert "old" not in names  # 5s 前的应被窗口过滤


def test_pipeline_summary_empty_returns_empty_list() -> None:
    monitor = SystemPerfMonitor.get()
    rows = pipeline_cpu_summary(monitor.pipeline_cpu_samples)
    assert rows == []


# ---------- 异常吞掉 ----------


def test_cpu_track_swallows_report_failure(monkeypatch) -> None:
    """SystemPerfMonitor 上报失败时 decorator 不应抛, 业务函数仍正常返回。"""
    @cpu_track("test_report_fail")
    def work() -> str:
        return "ok"

    # monkeypatch SystemPerfMonitor.get 抛异常
    import polymarket_trader.observability.cpu_track as ct
    original = ct._report

    def boom(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(ct, "_report", boom)
    # 应不抛
    result = work()
    assert result == "ok"
    monkeypatch.setattr(ct, "_report", original)


def test_cpu_track_propagates_function_exception() -> None:
    """函数本身抛异常应正常传播 (不被 decorator 吞), 但仍要上报。"""
    @cpu_track("test_exc_propagate")
    def work() -> None:
        raise ValueError("from function")

    with pytest.raises(ValueError, match="from function"):
        work()

    # 即使抛异常, finally 里仍应上报一次
    monitor = SystemPerfMonitor.get()
    assert len(monitor.pipeline_cpu_samples["test_exc_propagate"]) == 1
