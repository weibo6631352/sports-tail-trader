from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from polymarket_trader.runtime.status import SchedulerJob, SchedulerSnapshot

JobCallable = Callable[[], Coroutine[object, object, None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _JobState:
    name: str
    job: JobCallable
    priority: str
    interval_seconds: float | None
    tags: tuple[str, ...]
    enabled: bool
    run_immediately: bool
    paused: bool = False
    running: bool = False
    run_count: int = 0
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_duration_ms: float | None = None
    next_run_at: datetime | None = None
    last_error: str | None = None
    task: asyncio.Task[None] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def to_snapshot(self) -> SchedulerJob:
        return SchedulerJob(
            name=self.name,
            priority=self.priority,
            interval_seconds=self.interval_seconds,
            enabled=self.enabled,
            paused=self.paused,
            running=self.running,
            run_count=self.run_count,
            tags=self.tags,
            last_started_at=self.last_started_at,
            last_finished_at=self.last_finished_at,
            last_duration_ms=self.last_duration_ms,
            next_run_at=self.next_run_at,
            last_error=self.last_error,
        )


class Scheduler:
    """Named async scheduler used by M4 bootstrap and maintenance flows.

    Scheduler 只负责创建、唤醒和暂停任务，不做业务判断。降级由 supervisor 决定，
    scheduler 只暴露足够轻量的状态快照，避免 Admin / metrics 反向依赖真实 task 对象。
    """

    def __init__(self) -> None:
        self._jobs: dict[str, _JobState] = {}
        self._stop_event = asyncio.Event()

    def register_job(
        self,
        name: str,
        job: JobCallable,
        *,
        priority: str,
        interval_seconds: float | None = None,
        tags: tuple[str, ...] = (),
        start: bool = False,
        run_immediately: bool = False,
    ) -> None:
        if name in self._jobs:
            raise ValueError(f"scheduler job already registered: {name}")
        state = _JobState(
            name=name,
            job=job,
            priority=priority,
            interval_seconds=interval_seconds,
            tags=tags,
            enabled=start,
            run_immediately=run_immediately,
            next_run_at=_utc_now() if start and run_immediately else None,
        )
        self._jobs[name] = state
        if start:
            self.start_job(name)

    def start_job(self, name: str) -> None:
        state = self._jobs[name]
        state.enabled = True
        if state.next_run_at is None and state.interval_seconds is not None:
            # run_immediately=False: 首次运行延迟一个 interval，不在启动时立即跑。
            delay = 0.0 if state.run_immediately else state.interval_seconds
            state.next_run_at = _utc_now() + timedelta(seconds=delay)
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(self._run_job_loop(state), name=f"scheduler:{name}")
        state.wake.set()

    def start_all(self) -> None:
        for name in tuple(self._jobs):
            if self._jobs[name].enabled:
                self.start_job(name)

    def pause_job(self, name: str) -> None:
        state = self._jobs[name]
        state.paused = True
        state.wake.set()

    def resume_job(self, name: str) -> None:
        state = self._jobs[name]
        state.paused = False
        if state.interval_seconds is not None and state.next_run_at is None:
            state.next_run_at = _utc_now()
        state.wake.set()

    def trigger_now(self, name: str) -> None:
        state = self._jobs[name]
        state.enabled = True
        state.next_run_at = _utc_now()
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(self._run_job_loop(state), name=f"scheduler:{name}")
        state.wake.set()

    def stop_job(self, name: str) -> None:
        state = self._jobs[name]
        state.enabled = False
        state.paused = False
        state.next_run_at = None
        state.wake.set()
        if state.task is not None:
            state.task.cancel()

    async def shutdown(self) -> None:
        self._stop_event.set()
        tasks = [state.task for state in self._jobs.values() if state.task is not None]
        for state in self._jobs.values():
            state.enabled = False
            state.wake.set()
            if state.task is not None:
                state.task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def create_task(self, job: Callable[[], Coroutine[object, object, None]]) -> asyncio.Task[None]:
        return asyncio.create_task(job())

    def snapshot(self) -> SchedulerSnapshot:
        jobs = tuple(
            state.to_snapshot()
            for _, state in sorted(self._jobs.items(), key=lambda item: item[0])
        )
        return SchedulerSnapshot(jobs=jobs)

    async def _run_job_loop(self, state: _JobState) -> None:
        try:
            while not self._stop_event.is_set() and state.enabled:
                if state.paused:
                    await self._wait_for_wake(state, timeout_s=None)
                    continue

                timeout_s = self._seconds_until_next_run(state)
                if timeout_s is not None and timeout_s > 0:
                    await self._wait_for_wake(state, timeout_s=timeout_s)
                    continue

                await self._run_once(state)
                if state.interval_seconds is None:
                    state.next_run_at = None
                    await self._wait_for_wake(state, timeout_s=None)
                    continue
                state.next_run_at = _utc_now() + timedelta(seconds=state.interval_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            state.running = False
            state.task = None

    async def _run_once(self, state: _JobState) -> None:
        state.running = True
        started_at = _utc_now()
        state.last_started_at = started_at
        state.last_error = None
        try:
            await state.job()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.last_error = str(exc)
        finally:
            state.run_count += 1
            state.running = False
            finished_at = _utc_now()
            state.last_finished_at = finished_at
            # 直接记录当前这次运行的真实耗时；外部观测必须读这个字段，不能用
            # (last_finished_at - last_started_at) 算——并发触发或 start 已被下一
            # 轮覆盖时差值会是负数（N9）。
            state.last_duration_ms = (finished_at - started_at).total_seconds() * 1000.0

    async def _wait_for_wake(self, state: _JobState, *, timeout_s: float | None) -> None:
        state.wake.clear()
        if timeout_s is None:
            await state.wake.wait()
            return
        try:
            await asyncio.wait_for(state.wake.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return

    def _seconds_until_next_run(self, state: _JobState) -> float | None:
        if state.next_run_at is None:
            return None
        delta = (state.next_run_at - _utc_now()).total_seconds()
        if delta < 0:
            return 0.0
        return delta
