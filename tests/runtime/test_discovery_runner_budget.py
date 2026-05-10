from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    )
    live_state_hooks = SimpleNamespace(
        discovery_queries_for_live_games=lambda games: (
            DiscoveryQuery(
                name=f"live:{games[0].source_event_id}:oilers",
                params={"title_search": "oilers", "tag_slug": "sports"},
            ),
        ),
    )
    runtime = SimpleNamespace(
        extension=SimpleNamespace(hooks=hooks, live_state_hooks=live_state_hooks),
        sports_live_state_worker=SimpleNamespace(last_games=lambda: (game,)),
    )

    queries = discovery_runner._discovery_queries(runtime)

    assert queries[0].params == {"title_search": "oilers", "tag_slug": "sports"}
    assert queries[1].params == {"title_search": "nhl"}


def test_discovery_state_restarts_at_front_when_live_queries_are_added() -> None:
    state = discovery_runner.FullMarketDiscoveryState()
    broad_queries = (
        DiscoveryQuery.title_search("nba"),
        DiscoveryQuery.title_search("nhl"),
        DiscoveryQuery.title_search("mlb"),
    )

    assert state.next_query(broad_queries).name == "title_search:nba"
    assert state.next_query(broad_queries).name == "title_search:nhl"

    live_queries = (
        DiscoveryQuery(name="live_game:tennis:1:erhard nedic", params={"title_search": "erhard nedic"}),
        *broad_queries,
    )

    assert state.next_query(live_queries).name == "live_game:tennis:1:erhard nedic"


def test_live_event_expansion_uses_stale_live_metadata_event_slugs() -> None:
    now = datetime(2026, 4, 29, 9, 20, tzinfo=timezone.utc)
    state = discovery_runner.FullMarketDiscoveryState()
    state.live_event_expanded_at["atp-live-1"] = now - timedelta(seconds=31)
    state.live_event_expanded_at["atp-live-2"] = now
    runtime = SimpleNamespace(
        market_discovery_scan=state,
        entry_metadata_store=SimpleNamespace(
            records=lambda: (
                SimpleNamespace(
                    event_slug="atp-live-1",
                    live_state_phase="live",
                    live_state_payload={"status": "live"},
                ),
                SimpleNamespace(
                    event_slug="atp-live-2",
                    live_state_phase="live",
                    live_state_payload={"status": "live"},
                ),
                SimpleNamespace(
                    event_slug="kbo-scheduled",
                    live_state_phase="scheduled",
                    live_state_payload={"status": "scheduled"},
                ),
            )
        ),
    )

    assert discovery_runner._live_event_slugs_for_expansion(runtime, now=now) == ("atp-live-1",)
