"""aggregate_client 融合策略测试：
- majority vote (status)
- trusted_source ENDED 优先
- score 中位数
- race leader 取 trusted
- per-league priority 覆盖默认全局表
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    RaceState,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
)
from polymarket_trader.infra.sports.aggregate_client import SportsLiveAggregateClient

_OBSERVED = datetime(2026, 5, 11, tzinfo=timezone.utc)


def _team_event(*, source: str, status: SportsLiveGameStatus, home_score: int, away_score: int, league: str = "NBA") -> LiveEvent:
    return LiveEvent(
        source=source,
        source_event_id=f"{source}-1",
        kind=LiveEventKind.TEAM_MATCH,
        league=league,
        sport="basketball",
        participants=(
            Participant(role="home", name="Magic", score=home_score, short_name="Magic"),
            Participant(role="away", name="Pistons", score=away_score, short_name="Pistons"),
        ),
        status=status,
        observed_at=_OBSERVED,
        external_ids={"shared": "1"},
    )


async def _wrap(snap: SportsLiveSnapshot) -> SportsLiveSnapshot:
    return snap


def test_fusion_status_majority_vote_across_three_sources() -> None:
    e1 = _team_event(source="nba", status=SportsLiveGameStatus.LIVE, home_score=80, away_score=78)
    e2 = _team_event(source="espn", status=SportsLiveGameStatus.LIVE, home_score=82, away_score=78)
    e3 = _team_event(source="sofascore", status=SportsLiveGameStatus.PAUSED, home_score=80, away_score=78)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("nba", lambda: _wrap(SportsLiveSnapshot(source="nba", observed_at=_OBSERVED, events=(e1,)))),
                ("espn", lambda: _wrap(SportsLiveSnapshot(source="espn", observed_at=_OBSERVED, events=(e2,)))),
                ("sofascore", lambda: _wrap(SportsLiveSnapshot(source="sofascore", observed_at=_OBSERVED, events=(e3,)))),
            ),
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    assert len(snapshot.events) == 1
    fused = snapshot.events[0]
    # 2 票 LIVE vs 1 票 PAUSED → LIVE 胜出
    assert fused.status == SportsLiveGameStatus.LIVE
    # 被压制的 PAUSED 应在 source_conflicts 留痕
    paused_conflict = next(c for c in fused.source_conflicts if c.field == "status")
    assert paused_conflict.loser_value == SportsLiveGameStatus.PAUSED.value
    assert paused_conflict.decided_by in {"majority", "priority_tiebreak"}


def test_fusion_trusted_source_ended_overrides_other_live() -> None:
    """trusted（mlb）主张 ENDED；非 trusted 主张 LIVE → 取 ENDED。"""
    trusted = _team_event(source="mlb", status=SportsLiveGameStatus.ENDED, home_score=5, away_score=3, league="MLB")
    trusted_event = type(trusted)(
        **{**trusted.__dict__, "sport": "baseball"}  # tweak sport just to confirm pass-through
    ) if hasattr(trusted, "__dict__") else trusted
    other = _team_event(source="sofascore", status=SportsLiveGameStatus.LIVE, home_score=4, away_score=3, league="MLB")

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("mlb", lambda: _wrap(SportsLiveSnapshot(source="mlb", observed_at=_OBSERVED, events=(trusted,)))),
                ("sofascore", lambda: _wrap(SportsLiveSnapshot(source="sofascore", observed_at=_OBSERVED, events=(other,)))),
            ),
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    fused = snapshot.events[0]
    assert fused.status == SportsLiveGameStatus.ENDED
    status_conflict = next(c for c in fused.source_conflicts if c.field == "status")
    assert status_conflict.decided_by == "trusted_source"


def test_fusion_score_median_when_no_trusted_source() -> None:
    """非 trusted 源比分不同 → 取 per-side 中位数（防单源跳号）。"""
    e1 = _team_event(source="sofascore", status=SportsLiveGameStatus.LIVE, home_score=80, away_score=70)
    e2 = _team_event(source="thesportsdb", status=SportsLiveGameStatus.LIVE, home_score=82, away_score=70)
    e3 = _team_event(source="fotmob", status=SportsLiveGameStatus.LIVE, home_score=84, away_score=70)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("sofascore", lambda: _wrap(SportsLiveSnapshot(source="sofascore", observed_at=_OBSERVED, events=(e1,)))),
                ("thesportsdb", lambda: _wrap(SportsLiveSnapshot(source="thesportsdb", observed_at=_OBSERVED, events=(e2,)))),
                ("fotmob", lambda: _wrap(SportsLiveSnapshot(source="fotmob", observed_at=_OBSERVED, events=(e3,)))),
            ),
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    fused = snapshot.events[0]
    # 中位数 80,82,84 → 82
    assert fused.home.score == 82
    home_conflict = next(c for c in fused.source_conflicts if c.field == "home_score")
    assert home_conflict.decided_by == "median"


def test_fusion_score_trusted_source_overrides_when_present() -> None:
    """trusted（nba）有 score → 直接采纳 trusted score，不走中位数。"""
    trusted = _team_event(source="nba", status=SportsLiveGameStatus.LIVE, home_score=80, away_score=70)
    sofa = _team_event(source="sofascore", status=SportsLiveGameStatus.LIVE, home_score=84, away_score=70)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("nba", lambda: _wrap(SportsLiveSnapshot(source="nba", observed_at=_OBSERVED, events=(trusted,)))),
                ("sofascore", lambda: _wrap(SportsLiveSnapshot(source="sofascore", observed_at=_OBSERVED, events=(sofa,)))),
            ),
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    fused = snapshot.events[0]
    assert fused.home.score == 80
    home_conflict = next(c for c in fused.source_conflicts if c.field == "home_score")
    assert home_conflict.decided_by == "trusted_source"


def test_fusion_race_leader_takes_trusted_source() -> None:
    """两源对 F1 leader 意见不同 → 取 trusted（espn）的 leader_driver。"""

    race_trusted = LiveEvent(
        source="espn",
        source_event_id="r1",
        kind=LiveEventKind.RACE,
        sport="motorsport",
        league="F1",
        participants=(Participant(role="driver", name="Max Verstappen", position=1),),
        status=SportsLiveGameStatus.LIVE,
        observed_at=_OBSERVED,
        race_state=RaceState(leader_driver="Max Verstappen", leader_team="Red Bull"),
        external_ids={"espn": "r1"},
        event_name="Monaco GP",
    )
    race_other = LiveEvent(
        source="sofascore",
        source_event_id="rs1",
        kind=LiveEventKind.RACE,
        sport="motorsport",
        league="F1",
        participants=(Participant(role="driver", name="Lando Norris", position=1),),
        status=SportsLiveGameStatus.LIVE,
        observed_at=_OBSERVED,
        race_state=RaceState(leader_driver="Lando Norris", leader_team="McLaren"),
        external_ids={"espn": "r1"},  # share id so they merge
        event_name="Monaco GP",
    )

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("espn", lambda: _wrap(SportsLiveSnapshot(source="espn", observed_at=_OBSERVED, events=(race_trusted,)))),
                ("sofascore", lambda: _wrap(SportsLiveSnapshot(source="sofascore", observed_at=_OBSERVED, events=(race_other,)))),
            ),
            now_provider=lambda: _OBSERVED,
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    assert len(snapshot.events) == 1
    fused = snapshot.events[0]
    assert fused.race_state is not None
    assert fused.race_state.leader_driver == "Max Verstappen"
    conflict = next(c for c in fused.source_conflicts if c.field == "leader_driver")
    assert conflict.winner_source == "espn"
    assert conflict.decided_by == "trusted_source"


def test_fusion_per_league_priority_overrides_default() -> None:
    """league_source_priority 把 sofascore 顶到 NBA 第 1 → 选 sofascore 而非 nba（默认 nba 50 > sofa 30）。"""
    e_nba = _team_event(source="nba", status=SportsLiveGameStatus.LIVE, home_score=80, away_score=70)
    e_sofa = _team_event(source="sofascore", status=SportsLiveGameStatus.LIVE, home_score=82, away_score=70)

    async def run() -> SportsLiveSnapshot:
        client = SportsLiveAggregateClient(
            providers=(
                ("nba", lambda: _wrap(SportsLiveSnapshot(source="nba", observed_at=_OBSERVED, events=(e_nba,)))),
                ("sofascore", lambda: _wrap(SportsLiveSnapshot(source="sofascore", observed_at=_OBSERVED, events=(e_sofa,)))),
            ),
            now_provider=lambda: _OBSERVED,
            league_source_priority={"NBA": ("sofascore", "nba")},
            # 把 NBA 从默认 trusted_sources 里拿掉，避免触发 trusted_source path
            trusted_sources=(),
        )
        return await client.list_events()

    snapshot = asyncio.run(run())
    fused = snapshot.events[0]
    # league-aware：sofascore 排在 NBA 第 1 → primary 是 sofascore
    assert fused.source == "sofascore"
    assert set(fused.contributing_sources) == {"nba", "sofascore"}
