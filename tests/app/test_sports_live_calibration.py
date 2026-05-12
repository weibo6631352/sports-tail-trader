"""体育直播源校准 harness 单元测试。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polymarket_trader.app.sports_live_calibration import (
    SportsLiveCalibrationReport,
    run_calibration,
)
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
)
from polymarket_trader.infra.sports.aggregate_client import SportsLiveAggregateClient


_OBSERVED = datetime(2026, 5, 11, tzinfo=timezone.utc)


def _team_event(*, source: str, league: str, external_id: str | None) -> LiveEvent:
    return LiveEvent(
        source=source,
        source_event_id=f"{source}-1",
        kind=LiveEventKind.TEAM_MATCH,
        league=league,
        sport="basketball",
        participants=(
            Participant(role="home", name="Magic", display_name="Orlando Magic", short_name="Magic"),
            Participant(role="away", name="Pistons", display_name="Detroit Pistons", short_name="Pistons"),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=_OBSERVED,
        external_ids=({"shared": external_id} if external_id else {}),
    )


def _market(condition_id: str, market_slug: str, *, question: str) -> Market:
    return Market(
        condition_id=condition_id,
        market_slug=market_slug,
        market_question=question,
        event_title=question,
        event_slug=market_slug,
        category="Sports",
        tags=("NBA", "Basketball"),
        outcomes=(
            MarketOutcome(token_id="h", outcome="Orlando Magic"),
            MarketOutcome(token_id="a", outcome="Detroit Pistons"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def test_calibration_reports_id_merge_rate_when_two_sources_share_id() -> None:
    """两源共享 external_id → id_merge_rate > 0；text_fallback_rate = 0。"""
    espn = _team_event(source="espn", league="NBA", external_id="g1")
    nba = _team_event(source="nba", league="NBA", external_id="g1")

    async def provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(source="sports_live_aggregate", observed_at=_OBSERVED, events=(espn, nba))

    client = SportsLiveAggregateClient(
        providers=(("espn", provider),),  # 单 provider 直接返回 aggregate fixture
        now_provider=lambda: _OBSERVED,
    )

    def _match(market, events):
        return events[0] if events else None

    report = asyncio.run(
        run_calibration(
            aggregate_client=client,
            markets=(_market("c1", "magic-pistons", question="Will the Magic beat the Pistons?"),),
            match_live_event=_match,
        )
    )
    assert isinstance(report, SportsLiveCalibrationReport)
    assert report.matched_markets == 1
    assert report.id_merge_rate > 0
    assert report.text_fallback_rate == 0.0


def test_calibration_failure_in_source_records_failure_without_raising() -> None:
    """source 抛错时报告 failures 含错误码，不抛异常退出。"""

    async def failing() -> SportsLiveSnapshot:
        raise RuntimeError("provider_500")

    client = SportsLiveAggregateClient(
        providers=(("espn", failing),),
        now_provider=lambda: _OBSERVED,
    )

    def _match(market, events):
        return None

    report = asyncio.run(
        run_calibration(
            aggregate_client=client,
            markets=(_market("c1", "x", question="?"),),
            match_live_event=_match,
        )
    )
    # aggregate 仍返回 snapshot（失败源在 source_statuses 标 success=False），所以
    # rounds 个 fetch 全成功。但 source_metrics 应反映 failure。
    sources = {m.source: m for m in report.source_metrics}
    assert "espn" in sources
    assert sources["espn"].success is False
    assert sources["espn"].last_error is not None


def test_calibration_emits_silent_gap_when_match_rate_below_threshold() -> None:
    """matched/candidate < threshold → silent_gap 警告。"""
    espn = _team_event(source="espn", league="NBA", external_id=None)

    async def provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(source="sports_live_aggregate", observed_at=_OBSERVED, events=(espn,))

    client = SportsLiveAggregateClient(
        providers=(("espn", provider),),
        now_provider=lambda: _OBSERVED,
    )

    def _no_match(market, events):
        return None

    markets = tuple(
        _market(f"c{i}", f"slug-{i}", question=f"market-{i}")
        for i in range(4)
    )
    report = asyncio.run(
        run_calibration(
            aggregate_client=client,
            markets=markets,
            match_live_event=_no_match,
            silent_gap_threshold=0.5,
        )
    )
    assert report.matched_markets == 0
    gap_codes = {g.code for g in report.silent_gaps}
    assert "low_match_rate" in gap_codes


def test_calibration_detects_silent_source_when_peers_active() -> None:
    """source A 成功但 0 events；source B 有 NBA events → 报 source_silent_with_peers_active。"""

    nba_event = _team_event(source="nba", league="NBA", external_id=None)

    async def empty_espn() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(source="espn", observed_at=_OBSERVED, events=())

    async def nba_provider() -> SportsLiveSnapshot:
        return SportsLiveSnapshot(source="nba", observed_at=_OBSERVED, events=(nba_event,))

    client = SportsLiveAggregateClient(
        providers=(
            ("espn", empty_espn),
            ("nba", nba_provider),
        ),
        now_provider=lambda: _OBSERVED,
    )

    def _match(market, events):
        return events[0] if events else None

    report = asyncio.run(
        run_calibration(
            aggregate_client=client,
            markets=(_market("c1", "magic-pistons", question="Will the Magic beat the Pistons?"),),
            match_live_event=_match,
        )
    )
    gap_codes = {g.code for g in report.silent_gaps}
    assert "source_silent_with_peers_active" in gap_codes
