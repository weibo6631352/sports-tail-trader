from __future__ import annotations

from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.runtime.registry import MarketRegistry


def test_registry_slug_lookup_prefers_exact_market_slug_over_event_slug() -> None:
    registry = MarketRegistry()
    main_market = Market(
        condition_id="match-condition",
        market_slug="atp-player-a-player-b-2026-04-28",
        event_slug="atp-player-a-player-b-2026-04-28",
        outcomes=(
            MarketOutcome(token_id="home", outcome="Player A"),
            MarketOutcome(token_id="away", outcome="Player B"),
        ),
    )
    child_market = Market(
        condition_id="total-condition",
        market_slug="atp-player-a-player-b-2026-04-28-match-total-21pt5",
        event_slug="atp-player-a-player-b-2026-04-28",
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
    )

    registry.upsert(main_market)
    registry.upsert(child_market)

    assert registry.get_by_slug("atp-player-a-player-b-2026-04-28") == main_market
    assert registry.get_by_slug("atp-player-a-player-b-2026-04-28-match-total-21pt5") == child_market
