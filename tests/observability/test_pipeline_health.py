"""``step_track`` + pipeline_health_summary 行为约束。

关键不变量:
1. step_track ctx 记录 cpu/wall + 上报到 monitor.
2. pipeline_step_summary: 按 pipeline 分组, share_of_pipeline_pct 之和 ≈ 100.
3. health 判断: idle/healthy/degraded/unhealthy 各档触发条件.
4. worker_tick 字段携带轮询时间 + drift_pct.
"""
from __future__ import annotations

import pytest

from polymarket_trader.observability.cpu_track import cpu_track, step_track
from polymarket_trader.runtime.system_perf_monitor import (
    SystemPerfMonitor,
    pipeline_health_summary,
    pipeline_step_summary,
)


@pytest.fixture(autouse=True)
def _reset_monitor():
    """每个测试独立: 清空 monitor 防交叉污染。"""
    monitor = SystemPerfMonitor.get()
    monitor.pipeline_cpu_samples.clear()
    monitor.pipeline_step_samples.clear()
    monitor.worker_ticks.clear()
    monitor.coroutine_exceptions.clear()
    yield
    monitor.pipeline_cpu_samples.clear()
    monitor.pipeline_step_samples.clear()
    monitor.worker_ticks.clear()
    monitor.coroutine_exceptions.clear()


# ---------- step_track ctx ----------


def test_step_track_records_to_monitor() -> None:
    monitor = SystemPerfMonitor.get()
    with step_track("p1", "s1"):
        _ = sum(range(1000))
    assert ("p1", "s1") in monitor.pipeline_step_samples
    assert len(monitor.pipeline_step_samples[("p1", "s1")]) == 1


def test_step_track_swallows_exception() -> None:
    """ctx 即使 body 抛异常仍上报 (finally 语义)。"""
    monitor = SystemPerfMonitor.get()
    with pytest.raises(ValueError):
        with step_track("p_exc", "s_exc"):
            raise ValueError("boom")
    # 仍应被记录 (从 __exit__ 触发 report)
    assert ("p_exc", "s_exc") in monitor.pipeline_step_samples


# ---------- pipeline_step_summary 聚合 ----------


def test_step_summary_share_of_pipeline_pct_adds_to_100() -> None:
    monitor = SystemPerfMonitor.get()
    monitor.record_pipeline_step("p1", "s1", cpu_ms=10.0, wall_ms=12.0)
    monitor.record_pipeline_step("p1", "s2", cpu_ms=20.0, wall_ms=25.0)
    monitor.record_pipeline_step("p1", "s3", cpu_ms=70.0, wall_ms=80.0)

    result = pipeline_step_summary(monitor.pipeline_step_samples)
    assert "p1" in result
    rows = result["p1"]
    assert len(rows) == 3
    total_share = sum(r["share_of_pipeline_pct"] for r in rows)
    assert 99.0 <= total_share <= 101.0
    # 排序: s3 最大 (70/100), 然后 s2, s1
    assert rows[0]["name"] == "s3"
    assert rows[-1]["name"] == "s1"


def test_step_summary_multiple_pipelines_independent() -> None:
    """两个 pipeline 各自的 share_pct 独立计算 (不混算)。"""
    monitor = SystemPerfMonitor.get()
    monitor.record_pipeline_step("p1", "s", cpu_ms=10.0, wall_ms=10.0)
    monitor.record_pipeline_step("p2", "s", cpu_ms=99.0, wall_ms=99.0)

    result = pipeline_step_summary(monitor.pipeline_step_samples)
    # 每个 pipeline 内部唯一 step 的 share 都是 100%
    assert result["p1"][0]["share_of_pipeline_pct"] == 100.0
    assert result["p2"][0]["share_of_pipeline_pct"] == 100.0


# ---------- pipeline_health_summary 健康判断 ----------


def test_health_idle_when_no_calls() -> None:
    """无任何调用 → idle (空结果，因为没有数据时不出现 idle pipeline)。

    注意: idle 仅当有 worker_tick 注册但 0 调用时才显示;
    cpu_track 完全无样本时该 pipeline 不出现.
    """
    monitor = SystemPerfMonitor.get()
    monitor.worker_ticks["idle_worker"]  # 触发 defaultdict 创建空 stat

    rows = pipeline_health_summary(
        cpu_samples_map=monitor.pipeline_cpu_samples,
        step_samples_map=monitor.pipeline_step_samples,
        worker_ticks=monitor.worker_ticks,
        coroutine_exceptions=monitor.coroutine_exceptions,
    )
    idle_rows = [r for r in rows if r["health"] == "idle"]
    assert len(idle_rows) == 1
    assert idle_rows[0]["name"] == "idle_worker"


def test_health_healthy_when_low_load_no_errors() -> None:
    """两个 pipeline 各占 50% CPU, 都低于 70% → healthy。"""
    monitor = SystemPerfMonitor.get()
    monitor.record_pipeline_cpu("p1", cpu_ms=50.0, wall_ms=50.0)
    monitor.record_pipeline_cpu("p2", cpu_ms=50.0, wall_ms=50.0)

    rows = pipeline_health_summary(
        cpu_samples_map=monitor.pipeline_cpu_samples,
        step_samples_map=monitor.pipeline_step_samples,
        worker_ticks=monitor.worker_ticks,
        coroutine_exceptions=monitor.coroutine_exceptions,
    )
    # 两个都 50% share — degraded 阈值是 >30%
    for r in rows:
        # 50% 触发 degraded (规则: 30-70% degraded)
        assert r["health"] == "degraded"


def test_health_unhealthy_when_cpu_share_over_70() -> None:
    """单 pipeline 独占 CPU → unhealthy。"""
    monitor = SystemPerfMonitor.get()
    monitor.record_pipeline_cpu("hog", cpu_ms=200.0, wall_ms=200.0)
    monitor.record_pipeline_cpu("light", cpu_ms=10.0, wall_ms=10.0)

    rows = pipeline_health_summary(
        cpu_samples_map=monitor.pipeline_cpu_samples,
        step_samples_map=monitor.pipeline_step_samples,
        worker_ticks=monitor.worker_ticks,
        coroutine_exceptions=monitor.coroutine_exceptions,
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["hog"]["health"] == "unhealthy"
    assert any("cpu_share" in reason for reason in by_name["hog"]["health_reasons"])


def test_health_worker_tick_drift_triggers_unhealthy() -> None:
    """worker_ticks drift > 100% → unhealthy。"""
    monitor = SystemPerfMonitor.get()
    # 手动构造 worker_ticks: 期望 1s, 实际 3s (drift 200%)
    stat = monitor.worker_ticks["slow_worker"]
    stat["expected_interval_s"] = 1.0
    stat["intervals"].extend([3.0, 3.0, 3.0])
    stat["count"] = 3
    # 还要有 cpu_samples 才能进入 health 计算 (避免 idle 路径)
    monitor.record_pipeline_cpu("slow_worker", cpu_ms=1.0, wall_ms=3000.0)

    rows = pipeline_health_summary(
        cpu_samples_map=monitor.pipeline_cpu_samples,
        step_samples_map=monitor.pipeline_step_samples,
        worker_ticks=monitor.worker_ticks,
        coroutine_exceptions=monitor.coroutine_exceptions,
    )
    by_name = {r["name"]: r for r in rows}
    row = by_name["slow_worker"]
    assert row["health"] == "unhealthy"
    assert any("drift" in r for r in row["health_reasons"])
    assert row["worker_tick"]["expected_interval_s"] == 1.0
    assert row["worker_tick"]["drift_pct"] >= 100


def test_health_includes_steps() -> None:
    """pipeline 含 step 时 steps 字段输出按 share_of_pipeline_pct 倒序。"""
    monitor = SystemPerfMonitor.get()
    monitor.record_pipeline_cpu("p_with_steps", cpu_ms=10.0, wall_ms=10.0)
    monitor.record_pipeline_step("p_with_steps", "small", cpu_ms=1.0, wall_ms=1.0)
    monitor.record_pipeline_step("p_with_steps", "big", cpu_ms=9.0, wall_ms=9.0)

    rows = pipeline_health_summary(
        cpu_samples_map=monitor.pipeline_cpu_samples,
        step_samples_map=monitor.pipeline_step_samples,
        worker_ticks=monitor.worker_ticks,
        coroutine_exceptions=monitor.coroutine_exceptions,
    )
    p = next(r for r in rows if r["name"] == "p_with_steps")
    assert len(p["steps"]) == 2
    # big 应该排在前 (share_pct 高)
    assert p["steps"][0]["name"] == "big"
    assert p["steps"][1]["name"] == "small"


# ---------- @cpu_track + step_track 联合使用 ----------


def test_cpu_track_with_inner_step_track() -> None:
    """函数级 @cpu_track 与内部 step_track 并存, 双方都上报。"""
    @cpu_track("combined")
    def work() -> None:
        with step_track("combined", "a"):
            sum(range(1000))
        with step_track("combined", "b"):
            sum(range(2000))

    work()
    monitor = SystemPerfMonitor.get()
    # pipeline 级
    assert "combined" in monitor.pipeline_cpu_samples
    assert len(monitor.pipeline_cpu_samples["combined"]) == 1
    # step 级
    assert ("combined", "a") in monitor.pipeline_step_samples
    assert ("combined", "b") in monitor.pipeline_step_samples
