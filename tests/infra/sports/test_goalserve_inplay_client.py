"""GoalserveInplayClient 后台轮询 / demand-driven / 429 退避回归测试。

不发真实 HTTP——通过 monkeypatch ``_fetch_once`` 与 ``_client.get`` 验证：
  - per-sport 独立后台 Task 与缓存合并；
  - active_sports_provider 的 demand-driven 跳过；
  - HTTP 429 触发该 sport 单独退避，不波及其他 sport；
  - 后台 Task 退出后 list_events 探测并重启。
"""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime, timezone

import httpx
import pytest

from polymarket_trader.domain.sports_live import (
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
)
from polymarket_trader.infra.sports.goalserve_inplay_client import (
    SPORT_CODE_TO_INPLAY_KEYS,
    GoalserveInplayClient,
    _decode_feed,
)


def _now() -> datetime:
    return datetime(2026, 5, 22, 17, 25, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# feed 解码
# ---------------------------------------------------------------------------


def test_decode_feed_handles_gzip() -> None:
    payload = {"events": {}, "bm": "bet365"}
    gzipped = gzip.compress(json.dumps(payload).encode())
    assert _decode_feed(gzipped) == payload


def test_decode_feed_handles_plain_json() -> None:
    """部分代理会自动解压——直接传 JSON bytes 也要能解析。"""
    payload = {"events": {}, "bm": "bet365"}
    assert _decode_feed(json.dumps(payload).encode()) == payload


# ---------------------------------------------------------------------------
# demand-driven
# ---------------------------------------------------------------------------


def test_sport_code_to_inplay_keys_covers_all_eight_sports() -> None:
    """8 个 inplay 运动都要在 demand-driven 映射里有对应 feed token。"""
    all_tokens = set()
    for keys in SPORT_CODE_TO_INPLAY_KEYS.values():
        all_tokens |= set(keys)
    assert all_tokens == {
        "soccer",
        "basket",
        "tennis",
        "volleyball",
        "amfootball",
        "esports",
        "hockey",
        "baseball",
    }


@pytest.mark.asyncio
async def test_demand_driven_skips_undemanded_sport() -> None:
    """active_sports_provider 不含某 sport 时，该 sport 标记 idle、不抓取。"""
    fetched: list[str] = []

    client = GoalserveInplayClient(
        sports=("basket", "tennis"),
        poll_interval_s=1.05,
        now_provider=_now,
        # 只需求 basketball → 只有 basket token 被抓。
        active_sports_provider=lambda: frozenset({"basketball"}),
    )

    async def fake_fetch(sport: str) -> None:
        fetched.append(sport)

    client._fetch_once = fake_fetch  # type: ignore[method-assign]
    try:
        client._ensure_tasks_alive()
        # 让每个 sport 的轮询循环至少跑一轮判定。
        await asyncio.sleep(0.05)
        assert "tennis" not in fetched
        assert client._states["tennis"].idle is True
        assert client._states["basket"].idle is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_provider_polls_all_sports() -> None:
    """无 active_sports_provider 时全量轮询（向后兼容）。"""
    fetched: set[str] = set()
    client = GoalserveInplayClient(sports=("basket", "baseball"), now_provider=_now)

    async def fake_fetch(sport: str) -> None:
        fetched.add(sport)

    client._fetch_once = fake_fetch  # type: ignore[method-assign]
    try:
        client._ensure_tasks_alive()
        await asyncio.sleep(0.05)
        assert fetched == {"basket", "baseball"}
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 429 退避
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_429_triggers_per_sport_backoff() -> None:
    """HTTP 429 → 该 sport 进入退避、status 为 RATE_LIMITED，不波及其他 sport。"""
    client = GoalserveInplayClient(
        sports=("basket",),
        poll_interval_s=1.05,
        rate_limit_backoff_s=30.0,
        now_provider=_now,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client._fetch_once("basket")
        st = client._states["basket"]
        assert st.backoff_until is not None
        assert st.consecutive_failures == 1
        assert "429" in (st.last_error or "")

        snapshot = await client.list_events()
        status = next(
            s for s in snapshot.source_statuses if s.source == "goalserve_inplay:basket"
        )
        assert status.health == SportsLiveSourceHealth.RATE_LIMITED
        assert status.success is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_backoff_skips_fetch_until_expired() -> None:
    """退避未到期时轮询循环跳过抓取（不再打 HTTP）。"""
    calls: list[str] = []
    client = GoalserveInplayClient(
        sports=("basket",),
        poll_interval_s=1.05,
        rate_limit_backoff_s=999.0,
        now_provider=_now,
    )
    # 预置一个远未到期的退避。
    client._states["basket"].backoff_until = client._monotonic() + 999.0

    async def fake_fetch(sport: str) -> None:
        calls.append(sport)

    client._fetch_once = fake_fetch  # type: ignore[method-assign]
    try:
        client._ensure_tasks_alive()
        await asyncio.sleep(0.05)
        # 退避中——_fetch_once 不应被调用。
        assert calls == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_successful_fetch_clears_backoff() -> None:
    """成功抓取后 backoff / 失败计数清零。"""
    client = GoalserveInplayClient(sports=("baseball",), now_provider=_now)
    client._states["baseball"].backoff_until = client._monotonic() + 1.0
    client._states["baseball"].consecutive_failures = 5

    feed = {"events": {}, "bm": "bet365"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gzip.compress(json.dumps(feed).encode()))

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client._fetch_once("baseball")
        st = client._states["baseball"]
        assert st.backoff_until is None
        assert st.consecutive_failures == 0
        assert st.last_error is None
        assert st.last_success_at is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetch_records_http_date_header_as_server_clock() -> None:
    """成功抓取后从 HTTP Date header 解析 server_clock_at，暴露 server_clock_lag_s。"""
    client = GoalserveInplayClient(sports=("baseball",), now_provider=_now)
    feed = {"events": {}, "bm": "bet365"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(json.dumps(feed).encode()),
            headers={"Date": "Tue, 15 Nov 1994 12:45:26 GMT"},
        )

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client._fetch_once("baseball")
        st = client._states["baseball"]
        assert st.server_clock_at is not None
        # 1994-11-15T12:45:26 UTC
        assert st.server_clock_at.year == 1994
        assert st.server_clock_at.hour == 12
        # 暴露到 status：server_clock_lag_s > 0（date 是远古，差很大）
        status = client.inplay_per_sport_status()
        baseball_status = next(s for s in status if s["sport"] == "baseball")
        assert baseball_status["server_clock_lag_s"] is not None
        assert baseball_status["server_clock_lag_s"] > 0
        # transport_lag_s = last_success_at - server_clock_at（_now 是 2025-...
        # 大约 30 多年差），但同一行 logic 验证字段存在即可。
        assert baseball_status["transport_lag_s"] is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetch_handles_missing_date_header() -> None:
    """无 Date header 时 server_clock_at 保持 None，status 不崩。"""
    client = GoalserveInplayClient(sports=("baseball",), now_provider=_now)
    feed = {"events": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        # 显式覆盖默认 Date：httpx 默认会自动塞 Date，这里清空
        return httpx.Response(200, content=gzip.compress(json.dumps(feed).encode()))

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client._fetch_once("baseball")
        st = client._states["baseball"]
        # httpx MockTransport 不自动加 Date header → server_clock_at = None
        assert st.server_clock_at is None
        status = client.inplay_per_sport_status()
        baseball_status = next(s for s in status if s["sport"] == "baseball")
        assert baseball_status["server_clock_lag_s"] is None
        assert baseball_status["transport_lag_s"] is None
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 后台 Task 自愈
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_restarts_dead_task() -> None:
    """后台 Task 退出后 list_events 必须探测到并重启。"""
    client = GoalserveInplayClient(sports=("tennis",), now_provider=_now)

    async def fake_fetch(sport: str) -> None:
        await asyncio.sleep(3600)

    client._fetch_once = fake_fetch  # type: ignore[method-assign]
    try:
        # 模拟已退出的 Task。
        async def _done() -> None:
            return None

        dead = asyncio.create_task(_done())
        await dead
        client._states["tennis"].task = dead

        await client.list_events()
        new_task = client._states["tennis"].task
        assert new_task is not None
        assert new_task is not dead
        assert not new_task.done()
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# list_events 合并与观测
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_merges_per_sport_caches() -> None:
    """list_events 合并各 sport 缓存，返回单一 SportsLiveSnapshot。"""
    client = GoalserveInplayClient(sports=("basket", "baseball"), now_provider=_now)

    async def fake_fetch(sport: str) -> None:
        await asyncio.sleep(3600)

    client._fetch_once = fake_fetch  # type: ignore[method-assign]
    try:
        snapshot = await client.list_events()
        assert isinstance(snapshot, SportsLiveSnapshot)
        assert snapshot.source == "goalserve_inplay"
        sources = {s.source for s in snapshot.source_statuses}
        assert sources == {"goalserve_inplay:basket", "goalserve_inplay:baseball"}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_inplay_per_sport_status_shape() -> None:
    """inplay_per_sport_status 返回每 sport 的可观测字段。"""
    client = GoalserveInplayClient(sports=("basket",), now_provider=_now)
    try:
        rows = client.inplay_per_sport_status()
        assert len(rows) == 1
        row = rows[0]
        for key in ("sport", "type", "connected", "idle", "events", "poll_task_running"):
            assert key in row
        assert row["sport"] == "basket"
        assert row["type"] == "http"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetch_once_parses_real_feed_into_events() -> None:
    """端到端：MockTransport 回放真实 fixture → _fetch_once 填充解析后的 events。"""
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "inplay_basket.json"
    feed = json.loads(fixture.read_text())
    client = GoalserveInplayClient(sports=("basket",), now_provider=_now)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/inplay-basket.gz"
        return httpx.Response(200, content=gzip.compress(json.dumps(feed).encode()))

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client._fetch_once("basket")
        st = client._states["basket"]
        assert st.raw_count == len(feed["events"])
        assert len(st.events) == len(feed["events"])
        assert all(ev.source == "goalserve_inplay" for ev in st.events)
    finally:
        await client.aclose()
