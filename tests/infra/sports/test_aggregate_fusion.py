"""aggregate_client 测试：Goalserve 单源透传、超时容错、源状态上报。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
)
from polymarket_trader.infra.sports.aggregate_client import SportsLiveAggregateClient

_OBSERVED = datetime(2026, 5, 11, tzinfo=timezone.utc)


def _goalserve_event(*, status: SportsLiveGameStatus, home_score: int, away_score: int, league: str = "NBA") -> LiveEvent:
    return LiveEvent(
        source="goalserve",
        source_event_id="goalserve-1",
        kind=LiveEventKind.TEAM_MATCH,
        league=league,
        sport="basketball",
        participants=(
            Participant(role="home", name="Magic", score=home_score, short_name="Magic"),
            Participant(role="away", name="Pistons", score=away_score, short_name="Pistons"),
        ),
        status=status,
        observed_at=_OBSERVED,
        external_ids={"goalserve": "goalserve-1"},
    )


async def _wrap(snap: SportsLiveSnapshot) -> SportsLiveSnapshot:
    return snap


def test_single_source_goalserve_passthrough() -> None:
    """单 Goalserve 源 → aggregate 直接透传，无融合歧义。"""
    event = _goalserve_event(status=SportsLiveGameStatus.LIVE, home_score=80, away_score=78)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("goalserve", lambda: _wrap(SportsLiveSnapshot(source="goalserve", observed_at=_OBSERVED, events=(event,)))),
            ),
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    assert len(snapshot.events) == 1
    fused = snapshot.events[0]
    assert fused.source == "goalserve"
    assert fused.status == SportsLiveGameStatus.LIVE
    assert fused.home.score == 80
    assert fused.away.score == 78
    assert fused.contributing_sources == ("goalserve",)
    assert not fused.source_conflicts


def test_source_status_success_reported() -> None:
    """成功拉取时 source_statuses 包含 success=True、events_seen 正确。"""
    event = _goalserve_event(status=SportsLiveGameStatus.LIVE, home_score=50, away_score=40)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("goalserve", lambda: _wrap(SportsLiveSnapshot(source="goalserve", observed_at=_OBSERVED, events=(event,)))),
            ),
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    assert len(snapshot.source_statuses) == 1
    status = snapshot.source_statuses[0]
    assert status.source == "goalserve"
    assert status.success is True
    assert status.events_seen == 1
    assert status.last_error is None


def test_provider_failure_returns_empty_with_error_status() -> None:
    """provider 抛异常 → 返回空 events，source_statuses 标 success=False。"""

    async def failing() -> SportsLiveSnapshot:
        raise RuntimeError("connection_timeout")

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(("goalserve", failing),),
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    assert snapshot.events == ()
    assert len(snapshot.source_statuses) == 1
    status = snapshot.source_statuses[0]
    assert status.source == "goalserve"
    assert status.success is False
    assert status.last_error is not None


def test_multiple_events_all_pass_through() -> None:
    """多场比赛全部透传，event_id 不合并。"""
    e1 = LiveEvent(
        source="goalserve", source_event_id="gs-1", kind=LiveEventKind.TEAM_MATCH,
        league="NBA", sport="basketball",
        participants=(
            Participant(role="home", name="Lakers", score=100),
            Participant(role="away", name="Celtics", score=95),
        ),
        status=SportsLiveGameStatus.LIVE, observed_at=_OBSERVED,
    )
    e2 = LiveEvent(
        source="goalserve", source_event_id="gs-2", kind=LiveEventKind.TEAM_MATCH,
        league="NBA", sport="basketball",
        participants=(
            Participant(role="home", name="Heat", score=80),
            Participant(role="away", name="Bucks", score=82),
        ),
        status=SportsLiveGameStatus.ENDED, observed_at=_OBSERVED,
    )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("goalserve", lambda: _wrap(SportsLiveSnapshot(source="goalserve", observed_at=_OBSERVED, events=(e1, e2)))),
            ),
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    assert len(snapshot.events) == 2
    ids = {e.source_event_id for e in snapshot.events}
    assert ids == {"gs-1", "gs-2"}


# ---------------------------------------------------------------------------
# Helpers for two-source fusion tests
# ---------------------------------------------------------------------------

def _make_event(
    *,
    source: str,
    source_event_id: str,
    status: SportsLiveGameStatus,
    home_score: int,
    away_score: int,
    league: str = "NBA",
    sport: str = "basketball",
    home_name: str = "Lakers",
    away_name: str = "Celtics",
    home_short: str = "Lakers",
    away_short: str = "Celtics",
    baseball_state=None,
) -> LiveEvent:
    from polymarket_trader.domain.sports_live import BaseballGameState  # noqa: F401 – needed for typing

    kwargs: dict = dict(
        source=source,
        source_event_id=source_event_id,
        kind=LiveEventKind.TEAM_MATCH,
        league=league,
        sport=sport,
        participants=(
            Participant(role="home", name=home_name, score=home_score, short_name=home_short),
            Participant(role="away", name=away_name, score=away_score, short_name=away_short),
        ),
        status=status,
        observed_at=_OBSERVED,
        external_ids={source: source_event_id},
    )
    if baseball_state is not None:
        kwargs["baseball_state"] = baseball_state
    return LiveEvent(**kwargs)


def _two_source_client() -> SportsLiveAggregateClient:
    """Convenience factory – providers are overridden per test; we just need the instance."""
    return SportsLiveAggregateClient(
        providers=(),
        now_provider=lambda: _OBSERVED,
    )


async def _run_two_sources(
    gs_event: LiveEvent,
    ls_event: LiveEvent,
) -> SportsLiveSnapshot:
    client = SportsLiveAggregateClient(
        providers=(
            (
                "goalserve",
                lambda: _wrap(SportsLiveSnapshot(source="goalserve", observed_at=_OBSERVED, events=(gs_event,))),
            ),
            (
                "goalserve_livescore",
                lambda: _wrap(SportsLiveSnapshot(source="goalserve_livescore", observed_at=_OBSERVED, events=(ls_event,))),
            ),
        ),
        now_provider=lambda: _OBSERVED,
    )
    return await client.list_events()


# ---------------------------------------------------------------------------
# Two-source status: both LIVE → fused status is LIVE
# ---------------------------------------------------------------------------


def test_two_source_both_live_fused_status_is_live() -> None:
    gs = _make_event(source="goalserve", source_event_id="gs-1", status=SportsLiveGameStatus.LIVE, home_score=50, away_score=48)
    ls = _make_event(source="goalserve_livescore", source_event_id="ls-1", status=SportsLiveGameStatus.LIVE, home_score=50, away_score=48)

    snapshot = asyncio.run(_run_two_sources(gs, ls))

    assert len(snapshot.events) == 1
    fused = snapshot.events[0]
    assert fused.status == SportsLiveGameStatus.LIVE
    assert not fused.source_conflicts


# ---------------------------------------------------------------------------
# Trusted source ENDED override: goalserve ENDED, livescore LIVE → ENDED wins
# ---------------------------------------------------------------------------


def test_trusted_source_ended_overrides_livescore_live() -> None:
    gs = _make_event(source="goalserve", source_event_id="gs-1", status=SportsLiveGameStatus.ENDED, home_score=100, away_score=98)
    ls = _make_event(source="goalserve_livescore", source_event_id="ls-1", status=SportsLiveGameStatus.LIVE, home_score=100, away_score=98)

    snapshot = asyncio.run(_run_two_sources(gs, ls))

    fused = snapshot.events[0]
    assert fused.status == SportsLiveGameStatus.ENDED
    # conflict must be recorded with decided_by="trusted_source"
    status_conflicts = [c for c in fused.source_conflicts if c.field == "status"]
    assert len(status_conflicts) == 1
    conflict = status_conflicts[0]
    assert conflict.decided_by == "trusted_source"
    assert conflict.winner_source == "goalserve"
    assert conflict.loser_source == "goalserve_livescore"
    assert conflict.winner_value == SportsLiveGameStatus.ENDED.value
    assert conflict.loser_value == SportsLiveGameStatus.LIVE.value


# ---------------------------------------------------------------------------
# Two-source scores agree → no score conflicts
# ---------------------------------------------------------------------------


def test_two_sources_agree_on_score_no_conflicts() -> None:
    gs = _make_event(source="goalserve", source_event_id="gs-1", status=SportsLiveGameStatus.LIVE, home_score=60, away_score=55)
    ls = _make_event(source="goalserve_livescore", source_event_id="ls-1", status=SportsLiveGameStatus.LIVE, home_score=60, away_score=55)

    snapshot = asyncio.run(_run_two_sources(gs, ls))

    fused = snapshot.events[0]
    assert fused.home.score == 60
    assert fused.away.score == 55
    score_conflicts = [c for c in fused.source_conflicts if c.field in ("home_score", "away_score")]
    assert score_conflicts == []


# ---------------------------------------------------------------------------
# Two-source score disagree: goalserve is trusted → goalserve score wins
# ---------------------------------------------------------------------------


def test_trusted_source_wins_score_conflict() -> None:
    gs = _make_event(source="goalserve", source_event_id="gs-1", status=SportsLiveGameStatus.LIVE, home_score=72, away_score=68)
    ls = _make_event(source="goalserve_livescore", source_event_id="ls-1", status=SportsLiveGameStatus.LIVE, home_score=70, away_score=65)

    snapshot = asyncio.run(_run_two_sources(gs, ls))

    fused = snapshot.events[0]
    assert fused.home.score == 72
    assert fused.away.score == 68
    home_conflicts = [c for c in fused.source_conflicts if c.field == "home_score"]
    assert len(home_conflicts) == 1
    assert home_conflicts[0].decided_by == "trusted_source"
    assert home_conflicts[0].winner_source == "goalserve"
    assert home_conflicts[0].winner_value == 72


# ---------------------------------------------------------------------------
# Two-source score disagree, neither is trusted → median_low used
# ---------------------------------------------------------------------------


def test_neither_trusted_score_uses_median_low() -> None:
    # Use two non-trusted source names
    async def run() -> SportsLiveSnapshot:
        e1 = _make_event(source="source_a", source_event_id="a-1", status=SportsLiveGameStatus.LIVE, home_score=80, away_score=78)
        e2 = _make_event(source="source_b", source_event_id="b-1", status=SportsLiveGameStatus.LIVE, home_score=82, away_score=80)
        client = SportsLiveAggregateClient(
            providers=(
                ("source_a", lambda: _wrap(SportsLiveSnapshot(source="source_a", observed_at=_OBSERVED, events=(e1,)))),
                ("source_b", lambda: _wrap(SportsLiveSnapshot(source="source_b", observed_at=_OBSERVED, events=(e2,)))),
            ),
            trusted_sources=[],  # no trusted sources
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    fused = snapshot.events[0]
    # median_low([80, 82]) = 80; median_low([78, 80]) = 78
    assert fused.home.score == 80
    assert fused.away.score == 78
    home_conflicts = [c for c in fused.source_conflicts if c.field == "home_score"]
    assert len(home_conflicts) == 1
    assert home_conflicts[0].decided_by == "median"


# ---------------------------------------------------------------------------
# contributing_sources lists both sources when two sources contribute
# ---------------------------------------------------------------------------


def test_contributing_sources_contains_both_sources() -> None:
    gs = _make_event(source="goalserve", source_event_id="gs-1", status=SportsLiveGameStatus.LIVE, home_score=30, away_score=25)
    ls = _make_event(source="goalserve_livescore", source_event_id="ls-1", status=SportsLiveGameStatus.LIVE, home_score=30, away_score=25)

    snapshot = asyncio.run(_run_two_sources(gs, ls))

    fused = snapshot.events[0]
    assert set(fused.contributing_sources) == {"goalserve", "goalserve_livescore"}


# ---------------------------------------------------------------------------
# source_conflicts recorded when trusted ENDED overrides livescore LIVE
# ---------------------------------------------------------------------------


def test_source_conflicts_recorded_for_trusted_ended_override() -> None:
    gs = _make_event(source="goalserve", source_event_id="gs-1", status=SportsLiveGameStatus.ENDED, home_score=88, away_score=84)
    ls = _make_event(source="goalserve_livescore", source_event_id="ls-1", status=SportsLiveGameStatus.LIVE, home_score=88, away_score=84)

    snapshot = asyncio.run(_run_two_sources(gs, ls))

    fused = snapshot.events[0]
    assert fused.source_conflicts, "expected at least one conflict record"
    fields = {c.field for c in fused.source_conflicts}
    assert "status" in fields


# ---------------------------------------------------------------------------
# baseball_state carried from secondary source when primary lacks it
# ---------------------------------------------------------------------------


def test_baseball_state_carried_from_secondary_source() -> None:
    from polymarket_trader.domain.sports_live import BaseballGameState

    bs = BaseballGameState(current_inning=7, inning_half="top", outs=2)

    gs = _make_event(
        source="goalserve",
        source_event_id="gs-1",
        status=SportsLiveGameStatus.LIVE,
        home_score=3,
        away_score=2,
        league="MLB",
        sport="baseball",
        home_name="RedSox",
        away_name="Yankees",
        home_short="RedSox",
        away_short="Yankees",
    )
    ls = _make_event(
        source="goalserve_livescore",
        source_event_id="ls-1",
        status=SportsLiveGameStatus.LIVE,
        home_score=3,
        away_score=2,
        league="MLB",
        sport="baseball",
        home_name="RedSox",
        away_name="Yankees",
        home_short="RedSox",
        away_short="Yankees",
        baseball_state=bs,
    )

    snapshot = asyncio.run(_run_two_sources(gs, ls))

    fused = snapshot.events[0]
    assert fused.baseball_state is not None
    assert fused.baseball_state.current_inning == 7
    assert fused.baseball_state.inning_half == "top"
    assert fused.baseball_state.outs == 2
