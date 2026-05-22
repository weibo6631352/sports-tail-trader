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
async def test_demand_driven_fetches_only_active_sports() -> None:
    """active_sports_provider 返回子集时，只抓取该子集内的 sport。"""

    client = GoalserveLivescoreClient(
        api_key="test",
        sports=("hockey", "soccer", "esports"),
        active_sports_provider=lambda: frozenset({"hockey"}),
    )
    fetched: list[str] = []

    async def _fake_fetch(sport: str, observed_at: datetime) -> tuple[list, int]:
        fetched.append(sport)
        return [], 0

    client._fetch_sport = _fake_fetch  # type: ignore[method-assign]
    try:
        snapshot = await client._fetch_all_sports()
        assert snapshot is not None
        assert fetched == ["hockey"]
        # 未抓取的 sport 仍出现在状态快照里，标记 idle 而非 error。
        sources = {ss.source for ss in snapshot.source_statuses}
        assert sources == {"goalserve_livescore:hockey"}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_demand_driven_empty_set_preserves_cache() -> None:
    """provider 返回空集时不抓取，且保留上一份缓存（不被空快照覆盖）。"""

    sentinel = _empty_snapshot()
    client = GoalserveLivescoreClient(
        api_key="test",
        sports=("hockey",),
        active_sports_provider=lambda: frozenset(),
    )
    client._cache = sentinel
    fetched: list[str] = []

    async def _fake_fetch(sport: str, observed_at: datetime) -> tuple[list, int]:
        fetched.append(sport)
        return [], 0

    client._fetch_sport = _fake_fetch  # type: ignore[method-assign]
    try:
        result = await client._fetch_all_sports()
        assert result is None  # 本轮不抓取
        assert fetched == []
        # _poll_loop 在 None 时必须保留缓存，不覆盖。
        client._poll_interval_s = 0.01
        task = asyncio.create_task(client._poll_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert client._cache is sentinel
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_provider_polls_all_sports() -> None:
    """provider 为 None 时退回全量轮询（向后兼容回归）。"""

    client = GoalserveLivescoreClient(
        api_key="test",
        sports=("hockey", "soccer", "esports"),
    )
    fetched: list[str] = []

    async def _fake_fetch(sport: str, observed_at: datetime) -> tuple[list, int]:
        fetched.append(sport)
        return [], 0

    client._fetch_sport = _fake_fetch  # type: ignore[method-assign]
    try:
        snapshot = await client._fetch_all_sports()
        assert snapshot is not None
        assert sorted(fetched) == ["esports", "hockey", "soccer"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_demand_driven_first_call_empty_uses_empty_snapshot() -> None:
    """首次 list_events 时 provider 返回空集：无缓存可保留，用空快照兜底。"""

    client = GoalserveLivescoreClient(
        api_key="test",
        sports=("hockey",),
        active_sports_provider=lambda: frozenset(),
    )
    try:
        snapshot = await client.list_events()
        assert snapshot.events == ()
        assert client._cache is snapshot
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
