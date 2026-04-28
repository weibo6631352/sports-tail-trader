from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveTeam,
)
from strategies.current.live_state import match_sports_live_game


def test_live_state_match_requires_market_date_when_both_sides_have_dates() -> None:
    market = _market("nba-phi-bos-2026-05-02")
    game = _game(start_time_utc="2026-04-28T23:30:00Z")

    assert match_sports_live_game(market, game) is None


def test_live_state_match_accepts_same_date_team_match() -> None:
    market = _market("nba-phi-bos-2026-04-28")
    game = _game(start_time_utc="2026-04-28T23:30:00Z")

    match = match_sports_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "401869408"


def test_live_state_metadata_preserves_tennis_state() -> None:
    market = Market(
        condition_id="tennis-condition",
        market_slug="wta-zolotar-papamic-2026-04-28-total-21pt5",
        market_question="Rada Zolotareva vs Despina Papamichail total games 21.5",
        event_title="Rada Zolotareva vs Despina Papamichail",
        event_slug="wta-zolotar-papamic-2026-04-28",
        category="Sports",
        tags=("WTA", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16078142",
        league="WTA 125K Huzhou, China Women Singles",
        home=SportsLiveTeam(name="Rada Zolotareva", score=0, short_name="R. Zolotareva"),
        away=SportsLiveTeam(name="Despina Papamichail", score=1, short_name="D. Papamichail"),
        status=SportsLiveGameStatus.LIVE,
        period="S2",
        observed_at=datetime(2026, 4, 28, 7, 0, tzinfo=timezone.utc),
        source_payload={
            "start_time_utc": "2026-04-28T06:25:00Z",
            "tennis_state": {
                "home_sets_won": 0,
                "away_sets_won": 1,
                "current_set": 2,
                "home_current_set_games": 0,
                "away_current_set_games": 0,
                "home_total_games": 4,
                "away_total_games": 6,
                "total_games": 10,
            },
        },
    )

    match = match_sports_live_game(market, game)

    assert match is not None
    assert match.metadata()["sports_tail_game"]["tennis_state"]["total_games"] == 10


def test_live_state_match_allows_tennis_adjacent_utc_date() -> None:
    market = Market(
        condition_id="tennis-condition",
        market_slug="wta-zolotar-papamic-2026-04-27-total-21pt5",
        market_question="Rada Zolotareva vs Despina Papamichail total games 21.5",
        event_title="Rada Zolotareva vs Despina Papamichail",
        event_slug="wta-zolotar-papamic-2026-04-27",
        category="Sports",
        tags=("WTA", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16078142",
        league="WTA 125K Huzhou, China Women Singles",
        home=SportsLiveTeam(name="Rada Zolotareva", score=0, short_name="R. Zolotareva"),
        away=SportsLiveTeam(name="Despina Papamichail", score=1, short_name="D. Papamichail"),
        status=SportsLiveGameStatus.LIVE,
        period="S2",
        observed_at=datetime(2026, 4, 28, 7, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "tennis",
            "start_time_utc": "2026-04-28T06:25:00Z",
            "tennis_state": {"home_total_games": 4, "away_total_games": 6, "total_games": 10},
        },
    )

    assert match_sports_live_game(market, game) is not None


def _market(slug: str) -> Market:
    return Market(
        condition_id=f"{slug}-condition",
        market_slug=slug,
        market_question="76ers vs Celtics",
        event_title="76ers vs Celtics",
        event_slug=slug,
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="home", outcome="76ers"),
            MarketOutcome(token_id="away", outcome="Celtics"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _game(*, start_time_utc: str) -> SportsLiveGame:
    return SportsLiveGame(
        source="espn",
        source_event_id="401869408",
        league="NBA",
        home=SportsLiveTeam(
            name="Celtics",
            score=0,
            display_name="Boston Celtics",
            abbreviation="BOS",
            short_name="Celtics",
        ),
        away=SportsLiveTeam(
            name="76ers",
            score=0,
            display_name="Philadelphia 76ers",
            abbreviation="PHI",
            short_name="76ers",
        ),
        status=SportsLiveGameStatus.SCHEDULED,
        period="STATUS_SCHEDULED",
        observed_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        source_payload={"start_time_utc": start_time_utc},
    )
