"""strategies.current.live_state.match_live_event race-kind 分支测试。

race kind 文本匹配走 leader_driver / top-3 driver / event_name，命中即返回
LiveMarketMatch，但 confidence 打 0.7 折避免压制 team-pair 匹配。
"""
from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    RaceState,
    SportsLiveGameStatus,
)
from strategies.current.live_state import best_live_match, match_live_event


_OBSERVED = datetime(2026, 5, 25, 14, tzinfo=timezone.utc)


def _race_event(*, leader: str = "Max Verstappen", event_name: str = "Monaco Grand Prix") -> LiveEvent:
    return LiveEvent(
        source="espn",
        source_event_id="r1",
        kind=LiveEventKind.RACE,
        league="F1",
        sport="motorsport",
        participants=(
            Participant(role="driver", name=leader, position=1),
            Participant(role="driver", name="Lando Norris", position=2),
            Participant(role="driver", name="Charles Leclerc", position=3),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=_OBSERVED,
        event_start_time=_OBSERVED,
        event_name=event_name,
        race_state=RaceState(leader_driver=leader, leader_team="Red Bull"),
    )


def _race_market(*, question: str, slug: str = "f1-monaco-winner") -> Market:
    return Market(
        condition_id="cond-1",
        market_slug=slug,
        market_question=question,
        event_title=question,
        event_slug=slug,
        category="Sports",
        tags=("F1", "Motorsport"),
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def test_race_match_hits_on_leader_driver_text() -> None:
    market = _race_market(question="Will Max Verstappen win the Monaco Grand Prix?")
    event = _race_event(leader="Max Verstappen")
    match = match_live_event(market, event)
    assert match is not None
    assert match.kind == LiveEventKind.RACE
    # Confidence 0.7 折扣后必须 > 0；leader 12 + event_name 4 = 16 score → 16/30 × 0.7 ≈ 0.373
    assert match.confidence > 0.0
    assert match.confidence < 1.0


def test_race_match_falls_back_to_top3_driver_when_leader_not_in_text() -> None:
    market = _race_market(question="Will Lando Norris finish on the podium at Monaco?")
    event = _race_event(leader="Max Verstappen")
    match = match_live_event(market, event)
    assert match is not None
    assert match.matched_home_alias == "Lando Norris"


def test_race_match_returns_none_when_no_driver_or_event_in_text() -> None:
    market = _race_market(question="Will Real Madrid win La Liga?", slug="laliga")
    event = _race_event()
    assert match_live_event(market, event) is None


def test_best_live_match_prefers_team_pair_over_race_kind() -> None:
    """同一 market 同时能匹配 team-pair 与 race 时，team-pair 因 confidence 更高胜出。"""

    market = Market(
        condition_id="cond-2",
        market_slug="nba-magic-pistons",
        market_question="Magic vs. Pistons winner",
        event_title="Magic vs. Pistons",
        event_slug="nba-magic-pistons",
        category="Sports",
        tags=("NBA", "Basketball"),
        outcomes=(
            MarketOutcome(token_id="home", outcome="Orlando Magic"),
            MarketOutcome(token_id="away", outcome="Detroit Pistons"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    team_event = LiveEvent(
        source="nba",
        source_event_id="g1",
        kind=LiveEventKind.TEAM_MATCH,
        league="NBA",
        sport="basketball",
        participants=(
            Participant(role="home", name="Magic", display_name="Orlando Magic", short_name="Magic"),
            Participant(role="away", name="Pistons", display_name="Detroit Pistons", short_name="Pistons"),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=_OBSERVED,
    )
    # Race event 中提到 Pistons —— 极小概率撞名，但要确保 team-pair 仍优先。
    race_event = _race_event(leader="Pistons")
    best = best_live_match(market, (team_event, race_event))
    assert best is not None
    assert best.kind == LiveEventKind.TEAM_MATCH


def test_race_match_event_name_only_keyword_still_hits() -> None:
    market = _race_market(question="Will anyone retire in the Monaco Grand Prix?")
    event = _race_event(leader="Unknown Driver Xyz", event_name="Monaco Grand Prix")
    match = match_live_event(market, event)
    assert match is not None
    assert "Monaco Grand Prix" in match.matched_away_alias
