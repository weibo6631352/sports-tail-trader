"""GoalserveLivescoreClient 后台轮询自愈回归测试。

历史故障：后台轮询任务在一次网络抖动后僵死，``list_events()`` 永久返回冻结
缓存，livescore 数据源静默断流 3 小时，系统对所有进行中比赛失明。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from polymarket_trader.domain.sports_live import SportsLiveSnapshot
from polymarket_trader.infra.sports.goalserve_livescore_client import (
    GoalserveLivescoreClient,
)


def _empty_snapshot() -> SportsLiveSnapshot:
    return SportsLiveSnapshot(
        source="goalserve_livescore",
        observed_at=datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc),
        events=(),
        source_statuses=(),
    )


@pytest.mark.asyncio
async def test_list_events_restarts_dead_poll_task() -> None:
    """后台轮询任务退出后，list_events 必须探测到并重启它。"""

    client = GoalserveLivescoreClient(api_key="test", sports=("hockey",))
    try:
        client._cache = _empty_snapshot()

        # 模拟已退出的轮询任务（崩溃后 done()=True）。
        async def _already_done() -> None:
            return None

        dead = asyncio.create_task(_already_done())
        await dead
        client._poll_task = dead

        # list_events 不应再阻塞 HTTP；它返回缓存并重启死掉的轮询任务。
        snapshot = await client.list_events()
        assert snapshot is client._cache

        assert client._poll_task is not None
        assert client._poll_task is not dead
        assert not client._poll_task.done()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_poll_loop_round_has_hard_timeout() -> None:
    """整轮抓取必须有硬超时，卡死的 fetch 不能让轮询永久僵死。"""

    client = GoalserveLivescoreClient(api_key="test", sports=("hockey",))
    client._poll_interval_s = 0.01
    client._fetch_round_timeout_s = 0.05

    hang_started = asyncio.Event()

    async def _hang() -> SportsLiveSnapshot:
        hang_started.set()
        await asyncio.sleep(3600)  # 永不返回
        return _empty_snapshot()

    client._fetch_all_sports = _hang  # type: ignore[method-assign]

    try:
        task = asyncio.create_task(client._poll_loop())
        await asyncio.wait_for(hang_started.wait(), timeout=1.0)
        # 超时后轮询循环必须存活并继续下一轮，而不是卡死。
        await asyncio.sleep(0.2)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        await client.aclose()
