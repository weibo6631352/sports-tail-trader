"""ExternalIdIndex.merge：跨源 ID 合并 + 传递合并 + 无 ID 走文本 fallback。"""
from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
)
from polymarket_trader.infra.sports.external_id_index import ExternalIdIndex

_OBSERVED = datetime(2026, 5, 11, tzinfo=timezone.utc)


def _event(source: str, *, event_id: str, espn_id: str | None = None, sofascore_id: str | None = None) -> LiveEvent:
    ids: dict[str, str] = {source: event_id}
    if espn_id is not None:
        ids["espn"] = espn_id
    if sofascore_id is not None:
        ids["sofascore"] = sofascore_id
    return LiveEvent(
        source=source,
        source_event_id=event_id,
        kind=LiveEventKind.TEAM_MATCH,
        league="NBA",
        sport="basketball",
        participants=(
            Participant(role="home", name="Magic"),
            Participant(role="away", name="Pistons"),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=_OBSERVED,
        external_ids=ids,
    )


def test_merge_groups_two_sources_sharing_external_id() -> None:
    espn = _event("espn", event_id="123", espn_id="123")
    sofa = _event("sofascore", event_id="zz", espn_id="123", sofascore_id="zz")
    report = ExternalIdIndex.merge((espn, sofa))
    assert report.id_merge_count == 1
    assert report.singleton_count == 0
    assert len(report.groups) == 1
    group = report.groups[0]
    assert set(group.indices) == {0, 1}
    assert "espn" in group.schemes_used or "sofascore" in group.schemes_used


def test_merge_does_transitive_union_across_three_sources() -> None:
    """A↔B 共享 espn id；B↔C 共享 sofascore id；并集应把 A,B,C 合到同一组。"""
    a = _event("nba", event_id="n1", espn_id="123")
    b = _event("espn", event_id="123", espn_id="123", sofascore_id="zz")
    c = _event("sofascore", event_id="zz", sofascore_id="zz")
    report = ExternalIdIndex.merge((a, b, c))
    assert report.id_merge_count == 1
    assert len(report.groups) == 1
    assert set(report.groups[0].indices) == {0, 1, 2}


def test_merge_keeps_events_without_overlap_as_singletons() -> None:
    """两个 event 都没有任何外部 ID 交集 → 各自 singleton。"""
    a = _event("espn", event_id="1")
    b = _event("nba", event_id="2")
    report = ExternalIdIndex.merge((a, b))
    assert report.id_merge_count == 0
    assert report.singleton_count == 2
    assert len(report.groups) == 2


def test_merge_uses_participant_external_ids_for_grouping() -> None:
    """两源 event 级 ID 没交集，但同一 home 球队带 nba external_id → 应合并。"""
    a = LiveEvent(
        source="espn",
        source_event_id="e1",
        kind=LiveEventKind.TEAM_MATCH,
        league="NBA",
        sport="basketball",
        participants=(
            Participant(role="home", name="Magic", external_ids={"nba": "team-1"}),
            Participant(role="away", name="Pistons", external_ids={"nba": "team-2"}),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=_OBSERVED,
        external_ids={"espn": "e1"},
    )
    b = LiveEvent(
        source="nba",
        source_event_id="n2",
        kind=LiveEventKind.TEAM_MATCH,
        league="NBA",
        sport="basketball",
        participants=(
            Participant(role="home", name="Magic", external_ids={"nba": "team-1"}),
            Participant(role="away", name="Pistons", external_ids={"nba": "team-2"}),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=_OBSERVED,
        external_ids={"nba": "n2"},
    )
    report = ExternalIdIndex.merge((a, b))
    assert report.id_merge_count == 1


def test_merge_on_empty_input_returns_empty_report() -> None:
    report = ExternalIdIndex.merge(())
    assert report.id_merge_count == 0
    assert report.singleton_count == 0
    assert report.groups == ()
