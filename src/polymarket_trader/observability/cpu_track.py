"""管道 CPU 占用追踪——按"管道/worker"维度量化真实 CPU time。

为什么用 ``time.process_time()`` 而非 ``time.perf_counter()``:
- ``perf_counter()`` 是 wall-clock 时间(墙上时间),含 ``await`` 期间的等待.
- ``process_time()`` 是整进程的 user + system CPU time,跨 ``await`` 不计入
  让出的部分——精确反映"这段代码实际占了多少 CPU 时间片".
- 单 event loop 单线程下,``process_time`` ≈ 协程实际跑的 CPU time.

用法:
    @cpu_track("market_ws_push")
    async def _emit_snapshot_update(self, ...):
        ...

    @cpu_track("derived_publisher")
    def refresh(self, snapshot):
        ...

decorator 自动 inspect coroutine, 同时支持 async/sync.
内部用 ``SystemPerfMonitor.record_pipeline_cpu`` 累计.

工程约束 (CLAUDE.md §7 P0 不阻塞):
- 追踪本身开销 ~1-2 μs (两次 process_time + 一次 dict 写入).
- record_pipeline_cpu 异常被吞掉, 不影响业务路径.
"""
from __future__ import annotations

import functools
import inspect
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class CpuStopwatch:
    """同时记 wall (perf_counter) + CPU (process_time) 的 context manager.

    退出时填充 ``wall_ms`` / ``cpu_ms`` 属性。

    用法:
        with CpuStopwatch() as sw:
            do_work()
        print(sw.cpu_ms, sw.wall_ms)
    """

    __slots__ = ("_wall_start", "_cpu_start", "wall_ms", "cpu_ms")

    def __init__(self) -> None:
        self.wall_ms: float = 0.0
        self.cpu_ms: float = 0.0
        self._wall_start: float = 0.0
        self._cpu_start: float = 0.0

    def __enter__(self) -> "CpuStopwatch":
        self._wall_start = time.perf_counter()
        self._cpu_start = time.process_time()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.wall_ms = (time.perf_counter() - self._wall_start) * 1000.0
        self.cpu_ms = (time.process_time() - self._cpu_start) * 1000.0


def _report(pipeline: str, cpu_ms: float, wall_ms: float) -> None:
    """安全上报 (异常吞掉, 不影响业务路径)。"""
    try:
        from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
        monitor = SystemPerfMonitor.get()
        if monitor is not None:
            monitor.record_pipeline_cpu(pipeline, cpu_ms=cpu_ms, wall_ms=wall_ms)
    except Exception as exc:
        # 追踪本身不应该让业务路径挂. 只记一次 warn (高频路径已被 monitor.unknown 过滤).
        logger.debug("cpu_track report failed pipeline=%s err=%s", pipeline, exc)


def cpu_track(pipeline: str) -> Callable[[F], F]:
    """函数级 CPU 追踪 decorator. 同时支持 async/sync.

    Args:
        pipeline: 管道标识 (如 "market_ws_push" / "candidates_evaluation").
            建议用 snake_case 短名, 跨多个被装饰函数共享同一 pipeline 即可聚合.
    """
    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                wall_start = time.perf_counter()
                cpu_start = time.process_time()
                try:
                    return await func(*args, **kwargs)
                finally:
                    cpu_ms = (time.process_time() - cpu_start) * 1000.0
                    wall_ms = (time.perf_counter() - wall_start) * 1000.0
                    try:
                        _report(pipeline, cpu_ms, wall_ms)
                    except Exception:
                        # 追踪上报失败不能影响业务函数 (即使 _report 自己被替换/挂掉).
                        pass
            return async_wrapper  # type: ignore[return-value]
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                wall_start = time.perf_counter()
                cpu_start = time.process_time()
                try:
                    return func(*args, **kwargs)
                finally:
                    cpu_ms = (time.process_time() - cpu_start) * 1000.0
                    wall_ms = (time.perf_counter() - wall_start) * 1000.0
                    try:
                        _report(pipeline, cpu_ms, wall_ms)
                    except Exception:
                        # 追踪上报失败不能影响业务函数 (即使 _report 自己被替换/挂掉).
                        pass
            return sync_wrapper  # type: ignore[return-value]
    return decorator


class step_track:  # noqa: N801 — ctx-mgr 小写遵循 contextmanager 风格
    """``with step_track(pipeline, step):`` — 在 pipeline 内部细分 step 耗时。

    与 ``@cpu_track`` 不同: pipeline 是函数整体, step 是内部段, 二者并行使用:

        @cpu_track("market_ws_push")               # 整体 CPU/wall
        async def _emit_snapshot_update(...):
            with step_track("market_ws_push", "history_record"):
                ...
            with step_track("market_ws_push", "derived_refresh"):
                ...

    每个 step 上报 (cpu_ms, wall_ms) 到 ``SystemPerfMonitor.pipeline_step_samples``.
    /runtime/pipeline-health 按 pipeline 分组展示各 step 的耗时分位 + 占比.
    """

    __slots__ = ("_pipeline", "_step", "_wall_start", "_cpu_start")

    def __init__(self, pipeline: str, step: str) -> None:
        self._pipeline = pipeline
        self._step = step

    def __enter__(self) -> "step_track":
        self._wall_start = time.perf_counter()
        self._cpu_start = time.process_time()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        cpu_ms = (time.process_time() - self._cpu_start) * 1000.0
        wall_ms = (time.perf_counter() - self._wall_start) * 1000.0
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            monitor = SystemPerfMonitor.get()
            if monitor is not None:
                monitor.record_pipeline_step(
                    self._pipeline, self._step, cpu_ms=cpu_ms, wall_ms=wall_ms,
                )
        except Exception:
            # step 追踪失败永不影响业务路径
            pass


async def track_async(pipeline: str, coro: Awaitable[Any]) -> Any:
    """即席 (ad-hoc) 包装一个 awaitable. 适合非 decorator 场景 (如临时 measure 某个 await).

    用法:
        result = await track_async("custom_pipeline", some_coro())
    """
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    try:
        return await coro
    finally:
        cpu_ms = (time.process_time() - cpu_start) * 1000.0
        wall_ms = (time.perf_counter() - wall_start) * 1000.0
        _report(pipeline, cpu_ms, wall_ms)


__all__ = ["CpuStopwatch", "cpu_track", "step_track", "track_async"]
