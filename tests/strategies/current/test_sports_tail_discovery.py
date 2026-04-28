from __future__ import annotations

from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveTeam,
)
from strategies.current.config import CurrentStrategyConfig
from strategies.current.discovery import build_live_game_discovery_queries


def test_live_game_discovery_queries_use_real_team_terms() -> None:
    game = SportsLiveGame(
        source="nhl",
        source_event_id="2025030185",
        league="NHL",
        home=SportsLiveTeam(
            name="Oilers",
            score=0,
            display_name="Edmonton Oilers",
            abbreviation="EDM",
            short_name="Oilers",
        ),
        away=SportsLiveTeam(
            name="Ducks",
            score=0,
            display_name="Anaheim Ducks",
            abbreviation="ANA",
            short_name="Ducks",
        ),
        status=SportsLiveGameStatus.SCHEDULED,
        period="FUT",
    )

    queries = build_live_game_discovery_queries(CurrentStrategyConfig(), (game,))
    title_searches = {str(query.params.get("title_search")) for query in queries}

    assert "oilers" in title_searches
    assert "ducks" in title_searches
    assert all(query.params.get("tag_slug") == "sports" for query in queries)


def test_live_game_discovery_queries_ignore_finished_games() -> None:
    game = SportsLiveGame(
        source="nhl",
        source_event_id="ended-game",
        league="NHL",
        home=SportsLiveTeam(name="Penguins", score=3),
        away=SportsLiveTeam(name="Flyers", score=2),
        status=SportsLiveGameStatus.ENDED,
        period="Final",
    )

    assert build_live_game_discovery_queries(CurrentStrategyConfig(), (game,)) == ()


def test_live_game_discovery_queries_skip_location_only_and_abbreviation_terms() -> None:
    game = SportsLiveGame(
        source="mlb",
        source_event_id="823473",
        league="MLB",
        home=SportsLiveTeam(
            name="Giants",
            score=0,
            display_name="San Francisco Giants",
            abbreviation="SF",
            short_name="Giants",
            location="San Francisco",
            aliases=("San Francisco", "SF"),
        ),
        away=SportsLiveTeam(
            name="Phillies",
            score=0,
            display_name="Philadelphia Phillies",
            abbreviation="PHI",
            short_name="Phillies",
            location="Philadelphia",
            aliases=("Philadelphia", "PHI"),
        ),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Scheduled",
    )

    queries = build_live_game_discovery_queries(CurrentStrategyConfig(), (game,))
    title_searches = {str(query.params.get("title_search")) for query in queries}

    assert "giants" in title_searches
    assert "san francisco giants" in title_searches
    assert "francisco" not in title_searches
    assert "san francisco" not in title_searches
    assert "sf" not in title_searches
    assert "phi" not in title_searches


def test_live_game_discovery_queries_prioritize_live_games_before_scheduled_games() -> None:
    scheduled = SportsLiveGame(
        source="nhl",
        source_event_id="scheduled-game",
        league="NHL",
        home=SportsLiveTeam(name="Bruins", score=0),
        away=SportsLiveTeam(name="Sabres", score=0),
        status=SportsLiveGameStatus.SCHEDULED,
        period="FUT",
    )
    live = SportsLiveGame(
        source="sofascore",
        source_event_id="live-tennis",
        league="WTA 125K Huzhou, China Women Singles",
        home=SportsLiveTeam(name="Rada Zolotareva", score=0, short_name="R. Zolotareva"),
        away=SportsLiveTeam(name="Despina Papamichail", score=1, short_name="D. Papamichail"),
        status=SportsLiveGameStatus.LIVE,
        period="S2",
    )

    queries = build_live_game_discovery_queries(
        CurrentStrategyConfig(sports_live_discovery_max_games=1, sports_live_discovery_max_queries=4),
        (scheduled, live),
    )
    title_searches = [str(query.params.get("title_search")) for query in queries]

    assert title_searches
    assert "zolotareva" in title_searches[0]
    assert all("bruins" not in value and "sabres" not in value for value in title_searches)


def test_live_game_discovery_queries_prioritize_polymarket_covered_tennis() -> None:
    itf_live = SportsLiveGame(
        source="sofascore",
        source_event_id="itf-live",
        league="ITF M15 Islamabad Men",
        home=SportsLiveTeam(name="Ivan Iutkin", score=0),
        away=SportsLiveTeam(name="Nikita Ianin", score=0),
        status=SportsLiveGameStatus.LIVE,
        period="S1",
        source_payload={"sport": "tennis"},
    )
    wta_live = SportsLiveGame(
        source="sofascore",
        source_event_id="wta-live",
        league="WTA 125K Huzhou, China Women Singles",
        home=SportsLiveTeam(name="Rada Zolotareva", score=0),
        away=SportsLiveTeam(name="Despina Papamichail", score=1),
        status=SportsLiveGameStatus.LIVE,
        period="S2",
        source_payload={"sport": "tennis"},
    )

    queries = build_live_game_discovery_queries(
        CurrentStrategyConfig(sports_live_discovery_max_games=1, sports_live_discovery_max_queries=4),
        (itf_live, wta_live),
    )
    title_searches = [str(query.params.get("title_search")) for query in queries]

    assert title_searches
    assert "zolotareva" in title_searches[0]
    assert all("iutkin" not in value and "ianin" not in value for value in title_searches)
