from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from polymarket_trader.domain.sports_live import (
    
    LiveEvent,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
    Participant,
    LiveEventKind,
)
from polymarket_trader.infra.sports import SportsLiveAggregateClient


def test_aggregate_client_prefers_live_source_over_scheduled_duplicate() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", lambda: _snapshot("espn", _game("espn", SportsLiveGameStatus.SCHEDULED, observed))),
                ("nba", lambda: _snapshot("nba", _game("nba", SportsLiveGameStatus.LIVE, observed + timedelta(seconds=1)))),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert snapshot.source == "sports_live_aggregate"
    assert len(snapshot.events) == 1
    assert snapshot.events[0].source == "nba"
    assert snapshot.events[0].status == SportsLiveGameStatus.LIVE
    assert [status.source for status in snapshot.source_statuses] == ["espn", "nba"]
    assert [status.events_seen for status in snapshot.source_statuses] == [1, 1]
    assert all(status.success for status in snapshot.source_statuses)


def test_aggregate_client_keeps_healthy_sources_when_one_source_fails() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)

    async def failing_provider() -> SportsLiveSnapshot:
        raise RuntimeError("source unavailable")

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", failing_provider),
                ("nhl", lambda: _snapshot("nhl", _game("nhl", SportsLiveGameStatus.LIVE, observed))),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert len(snapshot.events) == 1
    assert snapshot.events[0].source == "nhl"
    assert [(status.source, status.success, status.events_seen) for status in snapshot.source_statuses] == [
        ("espn", False, 0),
        ("nhl", True, 1),
    ]
    assert "source unavailable" in (snapshot.source_statuses[0].last_error or "")


def test_aggregate_client_keeps_healthy_sources_when_one_source_hangs() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)

    async def hanging_provider() -> SportsLiveSnapshot:
        # 永久挂起直到被 provider_timeout 取消；不依赖 sleep 时间为正以避免 CI 抖动。
        await asyncio.Event().wait()
        return SportsLiveSnapshot(source="espn", observed_at=observed, events=())

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", hanging_provider),
                ("nhl", lambda: _snapshot("nhl", _game("nhl", SportsLiveGameStatus.LIVE, observed))),
            ),
            now_provider=lambda: observed,
            provider_timeout_s=0.01,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert len(snapshot.events) == 1
    assert snapshot.events[0].source == "nhl"
    assert [(status.source, status.success, status.events_seen) for status in snapshot.source_statuses] == [
        ("espn", False, 0),
        ("nhl", True, 1),
    ]
    assert "provider_timeout" in (snapshot.source_statuses[0].last_error or "")


def test_aggregate_client_reuses_last_successful_provider_snapshot_after_timeout() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    calls = 0

    async def flaky_sofascore_provider() -> SportsLiveSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            return await _snapshot(
                "sofascore",
                _game("sofascore", SportsLiveGameStatus.LIVE, observed),
            )
        # 永久挂起直到被 provider_timeout 取消；不依赖 sleep 时间为正以避免 CI 抖动。
        await asyncio.Event().wait()
        return SportsLiveSnapshot(source="sofascore", observed_at=observed, events=())

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("sofascore", flaky_sofascore_provider),
                ("nhl", lambda: _snapshot("nhl", _game("nhl", SportsLiveGameStatus.LIVE, observed))),
            ),
            now_provider=lambda: observed,
            provider_timeout_s=0.01,
        )
        await client.list_events()
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert len(snapshot.events) == 1
    assert snapshot.events[0].source == "nhl"
    assert [
        (status.source, status.success, status.health, status.events_seen)
        for status in snapshot.source_statuses
    ] == [
        ("sofascore", True, SportsLiveSourceHealth.CACHED, 1),
        ("nhl", True, SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA, 1),
    ]
    assert "provider_timeout" in (snapshot.source_statuses[0].last_error or "")


def test_aggregate_client_classifies_empty_rate_limited_and_failed_sources() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)

    async def rate_limited_provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(
            source="thesportsdb",
            observed_at=observed,
            events=(),
            source_statuses=(
                SportsLiveSourceStatus(
                    source="thesportsdb",
                    success=False,
                    health=SportsLiveSourceHealth.RATE_LIMITED,
                    events_seen=0,
                    observed_at=observed,
                    last_error="HTTP 429",
                ),
            ),
        )

    async def empty_provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(source="sofascore", observed_at=observed, events=())

    async def failing_provider() -> SportsLiveSnapshot:
        raise RuntimeError("timeout")

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("thesportsdb", rate_limited_provider),
                ("sofascore", empty_provider),
                ("espn", failing_provider),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert [
        (status.source, status.success, status.health, status.events_seen)
        for status in snapshot.source_statuses
    ] == [
        ("thesportsdb", False, SportsLiveSourceHealth.RATE_LIMITED, 0),
        ("sofascore", True, SportsLiveSourceHealth.SUCCESS_EMPTY, 0),
        ("espn", False, SportsLiveSourceHealth.FAILED, 0),
    ]


def test_aggregate_client_prefers_official_source_over_generic_duplicate() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("sofascore", lambda: _snapshot("sofascore", _game("sofascore", SportsLiveGameStatus.LIVE, observed + timedelta(seconds=2)))),
                ("nba", lambda: _snapshot("nba", _game("nba", SportsLiveGameStatus.LIVE, observed))),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert len(snapshot.events) == 1
    assert snapshot.events[0].source == "nba"


def test_aggregate_client_keeps_official_status_when_generic_source_conflicts() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    official_game = _game("nba", SportsLiveGameStatus.PAUSED, observed)
    generic_game = _game("sofascore", SportsLiveGameStatus.LIVE, observed + timedelta(seconds=2))

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("nba", lambda: _snapshot("nba", official_game)),
                ("sofascore", lambda: _snapshot("sofascore", generic_game)),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert len(snapshot.events) == 1
    assert snapshot.events[0].source == "nba"
    assert snapshot.events[0].status == SportsLiveGameStatus.PAUSED
    # 新模型 source_conflicts 是 ConflictRecord 一等字段，不再藏在 source_payload。
    conflicts = snapshot.events[0].source_conflicts
    assert any(
        c.field == "status"
        and c.winner_source == "nba"
        and c.loser_source == "sofascore"
        and c.winner_value == SportsLiveGameStatus.PAUSED.value
        and c.loser_value == SportsLiveGameStatus.LIVE.value
        for c in conflicts
    )


def test_aggregate_client_deduplicates_abbreviation_and_display_name_sources() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    espn_game = _game("espn", SportsLiveGameStatus.SCHEDULED, observed)
    nba_game = LiveEvent(
        
        participants=(Participant(
            role="home", name="Magic",
            score=87,
            display_name="Orlando Magic",
            abbreviation="ORL",
            short_name="Magic",
            location="Orlando",
        ), Participant(
            role="away", name="Pistons",
            score=85,
            display_name="Detroit Pistons",
            abbreviation="DET",
            short_name="Pistons",
            location="Detroit",
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="basketball",source="nba",
        source_event_id="nba-game-1",
        league="NBA",
        status=SportsLiveGameStatus.LIVE,
        period="Q4",
        seconds_remaining=188,
        observed_at=observed,
        raw_status="Q4 3:08",
    )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", lambda: _snapshot("espn", espn_game)),
                ("nba", lambda: _snapshot("nba", nba_game)),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert len(snapshot.events) == 1
    assert snapshot.events[0].source == "nba"


def test_aggregate_client_deduplicates_full_name_and_short_name_aliases() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    espn_game = LiveEvent(
        
        participants=(Participant(
            role="home", name="Utah Mammoth",
            score=0,
            display_name="Utah Mammoth",
            abbreviation="UTA",
            short_name="Mammoth",
        ), Participant(
            role="away", name="Vegas Golden Knights",
            score=2,
            display_name="Vegas Golden Knights",
            abbreviation="VGK",
            short_name="Golden Knights",
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="ice-hockey",source="espn",
        source_event_id="espn-nhl-1",
        league="NHL",
        status=SportsLiveGameStatus.LIVE,
        period="P1",
        seconds_remaining=2400,
        observed_at=observed,
        raw_status="STATUS_IN_PROGRESS",
        source_payload={"start_time_utc": "2026-04-28T01:30Z"},
    )
    nhl_game = LiveEvent(
        
        participants=(Participant(
            role="home", name="Mammoth",
            score=0,
            display_name="Mammoth",
            abbreviation="UTA",
            short_name="Mammoth",
        ), Participant(
            role="away", name="Golden Knights",
            score=2,
            display_name="Golden Knights",
            abbreviation="VGK",
            short_name="Golden Knights",
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="ice-hockey",source="nhl",
        source_event_id="nhl-1",
        league="NHL",
        status=SportsLiveGameStatus.PAUSED,
        period="P1",
        seconds_remaining=2837,
        observed_at=observed,
        raw_status="LIVE",
        source_payload={"start_time_utc": "2026-04-28T01:30Z"},
    )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", lambda: _snapshot("espn", espn_game)),
                ("nhl", lambda: _snapshot("nhl", nhl_game)),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert len(snapshot.events) == 1
    # 新模型按 league-aware 全局优先级（nhl 50 > espn 40）选融合主源；text-fallback dedup
    # 后两源被合并，contributing_sources 体现两源都参与。
    assert snapshot.events[0].source == "nhl"
    assert set(snapshot.events[0].contributing_sources) == {"espn", "nhl"}


def test_aggregate_client_keeps_same_teams_on_different_start_dates() -> None:
    observed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    ended_today = LiveEvent(
        
        participants=(Participant(
            role="home", name="Guardians",
            score=2,
            display_name="Cleveland Guardians",
            abbreviation="CLE",
        ), Participant(
            role="away", name="Rays",
            score=3,
            display_name="Tampa Bay Rays",
            abbreviation="TB",
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="baseball",source="mlb",
        source_event_id="mlb-1",
        league="MLB",
        status=SportsLiveGameStatus.ENDED,
        period="B9",
        observed_at=observed,
        event_start_time=datetime(2026, 4, 27, 22, 10, tzinfo=timezone.utc),
        raw_status="Final",
        source_payload={"game_date": "2026-04-27T22:10:00Z"},
    )
    scheduled_tomorrow = LiveEvent(
        
        participants=(Participant(
            role="home", name="Cleveland Guardians",
            score=0,
            display_name="Cleveland Guardians",
            abbreviation="CLE",
        ), Participant(
            role="away", name="Tampa Bay Rays",
            score=0,
            display_name="Tampa Bay Rays",
            abbreviation="TB",
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="baseball",source="sofascore",
        source_event_id="sofascore-1",
        league="MLB",
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=observed,
        event_start_time=datetime(2026, 4, 28, 22, 10, tzinfo=timezone.utc),
        raw_status="Not started",
        source_payload={"start_timestamp": 1777414200},
    )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("mlb", lambda: _snapshot("mlb", ended_today)),
                ("sofascore", lambda: _snapshot("sofascore", scheduled_tomorrow)),
            ),
            now_provider=lambda: observed,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())

    assert len(snapshot.events) == 2
    assert [game.source for game in snapshot.events] == ["mlb", "sofascore"]


def test_aggregate_client_cooldown_grows_exponentially_with_repeated_failures() -> None:
    """连续失败超过阈值后，每次失败的 cooldown 时长按 2^N 增长直到 cap。"""

    base = datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)
    # 每次失败 +1ms 推进，模拟连续轮询；threshold=2 表示第 2 次失败开始进入冷却。
    times_iter = iter(base + timedelta(milliseconds=i) for i in range(20))

    async def failing_provider() -> SportsLiveSnapshot:
        raise RuntimeError("network blip")

    async def run() -> SportsLiveAggregateClient:
        client = SportsLiveAggregateClient(
            providers=(("espn", failing_provider),),
            now_provider=lambda: next(times_iter),
            cooldown_base_s=10.0,
            cooldown_cap_s=40.0,
            eviction_s=10_000.0,
            cooldown_failure_threshold=2,
        )
        # 调用足够多次以让 cooldown_until 一直处于过期状态后再次触发失败。
        # 由于每次模拟时间只推进 1ms，cooldown_until 永远不会过期；
        # 所以从第 2 次失败之后，后续 list_games 都进入 cooldown 分支。
        await client.list_events()  # failure #1, no cooldown yet
        return client

    client = asyncio.run(run())
    cooldown = client._cooldowns["espn"]

    # 第 1 次失败：未进入冷却（threshold=2）。
    assert cooldown.consecutive_failures == 1
    assert cooldown.cooldown_until is None

    # 模拟更多次失败，时间不前进（仍在第 1 次失败之后的几毫秒），
    # 让冷却长度从 base*2^0 一直涨到 cap。
    async def push_failure(client: SportsLiveAggregateClient) -> None:
        # 推到 active 列表前手动清零 cooldown_until，以模拟 cooldown 到期可重试。
        client._cooldowns["espn"].cooldown_until = None
        await client.list_events()

    expected_delays = [10.0, 20.0, 40.0, 40.0]  # base=10 → 10, 20, 40, capped at 40
    for expected_delay in expected_delays:
        asyncio.run(push_failure(client))
        cooldown = client._cooldowns["espn"]
        assert cooldown.cooldown_until is not None
        delta = (cooldown.cooldown_until - cooldown.last_observed_at).total_seconds()
        assert delta == expected_delay


def test_aggregate_client_resets_cooldown_state_after_recovery() -> None:
    """成功一次后清空连续失败计数、cooldown_until、first_failure_at。"""

    base = datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)
    call_count = 0

    async def flaky_provider() -> SportsLiveSnapshot:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first failure")
        return SportsLiveSnapshot(source="espn", observed_at=base, events=())

    times = iter([base, base + timedelta(seconds=5)])

    async def run() -> SportsLiveAggregateClient:
        client = SportsLiveAggregateClient(
            providers=(("espn", flaky_provider),),
            now_provider=lambda: next(times),
            cooldown_base_s=60.0,
            cooldown_failure_threshold=1,
        )
        await client.list_events()  # failure → cooldown_until set
        # 强制清掉 cooldown_until 让下一轮真正调用 provider 而不是跳过。
        client._cooldowns["espn"].cooldown_until = None
        await client.list_events()  # success → state should reset
        return client

    client = asyncio.run(run())
    cooldown = client._cooldowns["espn"]
    assert cooldown.consecutive_failures == 0
    assert cooldown.cooldown_until is None
    assert cooldown.first_failure_at is None
    assert cooldown.evicted is False


def test_aggregate_client_skips_provider_during_active_cooldown() -> None:
    base = datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)
    call_count = 0

    async def failing_provider() -> SportsLiveSnapshot:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("flaky")

    async def healthy_provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(source="nhl", observed_at=base, events=(_game("nhl", SportsLiveGameStatus.LIVE, base),))

    times = iter([base, base + timedelta(seconds=1), base + timedelta(seconds=2)])

    async def run() -> list[SportsLiveSnapshot]:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", failing_provider),
                ("nhl", healthy_provider),
            ),
            now_provider=lambda: next(times),
            cooldown_base_s=300.0,
            cooldown_failure_threshold=1,
        )
        return [await client.list_events() for _ in range(3)]

    snapshots = asyncio.run(run())

    # 第 1 轮失败触发立即冷却（threshold=1）；第 2、3 轮处于冷却期不再调用。
    assert call_count == 1
    cooldown_status = next(s for s in snapshots[1].source_statuses if s.source == "espn")
    assert cooldown_status.health == SportsLiveSourceHealth.COOLDOWN
    assert cooldown_status.cooldown_until is not None
    assert cooldown_status.success is False


async def _snapshot(source: str, game: LiveEvent) -> SportsLiveSnapshot:
    # 兜底 observed_at 用固定瞬间，避免依赖墙钟。
    fallback_observed = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)
    return SportsLiveSnapshot(
        source=source,
        observed_at=game.observed_at or fallback_observed,
        events=(game,),
    )


def _game(source: str, status: SportsLiveGameStatus, observed_at: datetime) -> LiveEvent:
    return LiveEvent(
        
        participants=(Participant(
            role="home", name="Magic",
            score=78,
            display_name="Orlando Magic",
            abbreviation="ORL",
            short_name="Magic",
            location="Orlando",
        ), Participant(
            role="away", name="Pistons",
            score=76,
            display_name="Detroit Pistons",
            abbreviation="DET",
            short_name="Pistons",
            location="Detroit",
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="basketball",source=source,
        source_event_id=f"{source}-game-1",
        league="NBA",
        status=status,
        period="Q4" if status == SportsLiveGameStatus.LIVE else "STATUS_SCHEDULED",
        seconds_remaining=524 if status == SportsLiveGameStatus.LIVE else None,
        observed_at=observed_at,
        raw_status=status.value,
    )
