"""best_live_match 跨源赔率接入回归测试。

inplay GZIP feed 事件带 goalserve_odds，livescore 事件不带；两源名字格式不同
在 aggregate 融合时分不到一组。best_live_match 选中 livescore 事件后，必须从
同样匹配到该 market 的 inplay 事件把赔率接上，否则策略拿不到盘中赔率。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
)
from strategies.current.live_state import best_live_match


def _market() -> SimpleNamespace:
    return SimpleNamespace(
        market_question="Canadiens vs. Hurricanes",
        market_name=None,
        market_slug="nhl-mon-car-2026-05-21",
        event_title="Canadiens vs. Hurricanes",
        event_slug="nhl-mon-car-2026-05-21",
        category=None,
        tags=("NHL", "nhl", "Hockey"),
        outcomes=(),
        game_start_time=datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc),
    )


def _event(source: str, odds: dict | None) -> LiveEvent:
    return LiveEvent(
        source=source,
        source_event_id=f"{source}-1",
        kind=LiveEventKind.TEAM_MATCH,
        league="USA: NHL",
        sport="hockey",
        participants=(
            Participant(
                role="home", name="Carolina Hurricanes", score=1,
                location="Carolina", team="Hurricanes",
            ),
            Participant(
                role="away", name="Montreal Canadiens", score=4,
                location="Montreal", team="Canadiens",
            ),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=datetime(2026, 5, 22, 0, 56, tzinfo=timezone.utc),
        source_payload={"goalserve_odds": odds} if odds is not None else {},
    )


def test_best_live_match_carries_inplay_odds() -> None:
    odds = {"moneyline": {"home_implied_prob": "0.20", "away_implied_prob": "0.82"}}
    livescore_event = _event("goalserve_livescore", odds=None)
    inplay_event = _event("goalserve", odds=odds)

    match = best_live_match(_market(), (livescore_event, inplay_event))
    assert match is not None
    # 赔率必须接上（无论胜出的是哪个源事件）。
    assert match.event.source_payload.get("goalserve_odds") == odds


def test_best_live_match_no_odds_when_no_inplay_event() -> None:
    livescore_event = _event("goalserve_livescore", odds=None)
    match = best_live_match(_market(), (livescore_event,))
    assert match is not None
    assert not match.event.source_payload.get("goalserve_odds")
