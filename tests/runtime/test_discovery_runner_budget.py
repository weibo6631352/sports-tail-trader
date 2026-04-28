from __future__ import annotations

from types import SimpleNamespace

from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveTeam,
)
from polymarket_trader.extension_api import DiscoveryQuery
from polymarket_trader.runtime import discovery_runner


def test_full_market_discovery_defaults_keep_background_sla() -> None:
    assert discovery_runner._MARKET_DISCOVERY_REQUEST_BUDGET_PER_TICK == 2
    assert discovery_runner._MARKET_DISCOVERY_MAX_RUNTIME_MS == 200.0
    assert discovery_runner.MARKET_DISCOVERY_TICK_SECONDS == 0.5


def test_discovery_queries_prioritize_live_game_queries_before_broad_queries() -> None:
    game = SportsLiveGame(
        source="nhl",
        source_event_id="2025030185",
        league="NHL",
        home=SportsLiveTeam(name="Oilers", score=0, display_name="Edmonton Oilers"),
        away=SportsLiveTeam(name="Ducks", score=0, display_name="Anaheim Ducks"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="FUT",
    )
    hooks = SimpleNamespace(
        discovery_queries=lambda: (DiscoveryQuery.title_search("nhl"),),
        discovery_queries_for_live_games=lambda games: (
            DiscoveryQuery(
                name=f"live:{games[0].source_event_id}:oilers",
                params={"title_search": "oilers", "tag_slug": "sports"},
            ),
        ),
    )
    runtime = SimpleNamespace(
        extension=SimpleNamespace(hooks=hooks),
        sports_live_state_worker=SimpleNamespace(last_games=lambda: (game,)),
    )

    queries = discovery_runner._discovery_queries(runtime)

    assert queries[0].params == {"title_search": "oilers", "tag_slug": "sports"}
    assert queries[1].params == {"title_search": "nhl"}
