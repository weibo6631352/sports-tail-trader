from __future__ import annotations

from polymarket_trader.domain.sports_live import (
    
    LiveEvent,
    SportsLiveGameStatus,
    Participant,
    LiveEventKind,
)
from strategies.current.config import CurrentStrategyConfig
from strategies.current.discovery import build_live_event_discovery_queries


def test_live_game_discovery_queries_use_real_team_terms() -> None:
    game = LiveEvent(
        
        participants=(Participant(
            role="home", name="Oilers",
            score=0,
            display_name="Edmonton Oilers",
            abbreviation="EDM",
            short_name="Oilers",
        ), Participant(
            role="away", name="Ducks",
            score=0,
            display_name="Anaheim Ducks",
            abbreviation="ANA",
            short_name="Ducks",
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="ice-hockey",source="nhl",
        source_event_id="2025030185",
        league="NHL",
        status=SportsLiveGameStatus.SCHEDULED,
        period="FUT",
    )

    queries = build_live_event_discovery_queries(CurrentStrategyConfig(), (game,))
    title_searches = {str(query.params.get("title_search")) for query in queries}

    assert "oilers" in title_searches
    assert "ducks" in title_searches
    assert all(query.params.get("tag_slug") == "sports" for query in queries)


def test_live_game_discovery_queries_put_matchup_terms_before_single_team_terms() -> None:
    game = LiveEvent(
        
        participants=(Participant(
            role="home", name="Giants",
            score=0,
            display_name="San Francisco Giants",
            short_name="Giants",
        ), Participant(
            role="away", name="Phillies",
            score=0,
            display_name="Philadelphia Phillies",
            short_name="Phillies",
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="baseball",source="mlb",
        source_event_id="823473",
        league="MLB",
        status=SportsLiveGameStatus.LIVE,
        period="Top 9",
    )

    queries = build_live_event_discovery_queries(CurrentStrategyConfig(), (game,))
    title_searches = [str(query.params.get("title_search")) for query in queries]

    assert title_searches[:2] == ["giants phillies", "phillies giants"]
    assert "giants" in title_searches
    assert "phillies" in title_searches


def test_live_game_discovery_queries_ignore_finished_games() -> None:
    game = LiveEvent(
        
        participants=(Participant(role="home", name="Penguins", score=3), Participant(role="away", name="Flyers", score=2),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="ice-hockey",source="nhl",
        source_event_id="ended-game",
        league="NHL",
        status=SportsLiveGameStatus.ENDED,
        period="Final",
    )

    assert build_live_event_discovery_queries(CurrentStrategyConfig(), (game,)) == ()


def test_live_game_discovery_queries_skip_location_only_and_abbreviation_terms() -> None:
    game = LiveEvent(
        
        participants=(Participant(
            role="home", name="Giants",
            score=0,
            display_name="San Francisco Giants",
            abbreviation="SF",
            short_name="Giants",
            location="San Francisco",
            aliases=("San Francisco", "SF"),
        ), Participant(
            role="away", name="Phillies",
            score=0,
            display_name="Philadelphia Phillies",
            abbreviation="PHI",
            short_name="Phillies",
            location="Philadelphia",
            aliases=("Philadelphia", "PHI"),
        ),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="baseball",source="mlb",
        source_event_id="823473",
        league="MLB",
        status=SportsLiveGameStatus.SCHEDULED,
        period="Scheduled",
    )

    queries = build_live_event_discovery_queries(CurrentStrategyConfig(), (game,))
    title_searches = {str(query.params.get("title_search")) for query in queries}

    assert "giants" in title_searches
    assert "san francisco giants" in title_searches
    assert "francisco" not in title_searches
    assert "san francisco" not in title_searches
    assert "sf" not in title_searches
    assert "phi" not in title_searches


def test_live_game_discovery_queries_prioritize_live_games_before_scheduled_games() -> None:
    scheduled = LiveEvent(
        
        participants=(Participant(role="home", name="Bruins", score=0), Participant(role="away", name="Sabres", score=0),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="ice-hockey",source="nhl",
        source_event_id="scheduled-game",
        league="NHL",
        status=SportsLiveGameStatus.SCHEDULED,
        period="FUT",
    )
    live = LiveEvent(
        
        participants=(Participant(role="home", name="Rada Zolotareva", score=0, short_name="R. Zolotareva"), Participant(role="away", name="Despina Papamichail", score=1, short_name="D. Papamichail"),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="",source="sofascore",
        source_event_id="live-tennis",
        league="WTA 125K Huzhou, China Women Singles",
        status=SportsLiveGameStatus.LIVE,
        period="S2",
    )

    queries = build_live_event_discovery_queries(
        CurrentStrategyConfig(tail_live_discovery_max_games=1, tail_live_discovery_max_queries=4),
        (scheduled, live),
    )
    title_searches = [str(query.params.get("title_search")) for query in queries]

    assert title_searches
    assert "zolotareva" in title_searches[0]
    assert all("bruins" not in value and "sabres" not in value for value in title_searches)


def test_live_game_discovery_queries_prioritize_polymarket_covered_tennis() -> None:
    itf_live = LiveEvent(
        
        participants=(Participant(role="home", name="Ivan Iutkin", score=0), Participant(role="away", name="Nikita Ianin", score=0),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="",source="sofascore",
        source_event_id="itf-live",
        league="ITF M15 Islamabad Men",
        status=SportsLiveGameStatus.LIVE,
        period="S1",
        source_payload={"sport": "tennis"},
    )
    wta_live = LiveEvent(
        
        participants=(Participant(role="home", name="Rada Zolotareva", score=0), Participant(role="away", name="Despina Papamichail", score=1),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="",source="sofascore",
        source_event_id="wta-live",
        league="WTA 125K Huzhou, China Women Singles",
        status=SportsLiveGameStatus.LIVE,
        period="S2",
        source_payload={"sport": "tennis"},
    )

    queries = build_live_event_discovery_queries(
        CurrentStrategyConfig(tail_live_discovery_max_games=1, tail_live_discovery_max_queries=4),
        (itf_live, wta_live),
    )
    title_searches = [str(query.params.get("title_search")) for query in queries]

    assert title_searches
    assert "zolotareva" in title_searches[0]
    assert all("iutkin" not in value and "ianin" not in value for value in title_searches)
