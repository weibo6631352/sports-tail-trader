from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.infra.sports.season_odds_client import (
    parse_theoddsapi_outrights_payload,
)


def test_parses_outrights_with_devig() -> None:
    payload = [
        {
            "id": "nba-championship-2026",
            "sport_key": "basketball_nba_championship_winner",
            "commence_time": "2026-10-01T00:00:00Z",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "markets": [
                        {
                            "key": "outrights",
                            "outcomes": [
                                {"name": "Boston Celtics", "price": 3.0},
                                {"name": "Denver Nuggets", "price": 5.0},
                                {"name": "Other", "price": 1.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "fanduel",
                    "markets": [
                        {
                            "key": "outrights",
                            "outcomes": [
                                {"name": "Boston Celtics", "price": 3.2},
                                {"name": "Denver Nuggets", "price": 4.8},
                                {"name": "Other", "price": 1.5},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    snapshot = parse_theoddsapi_outrights_payload(
        payload,
        market_key="basketball_nba_championship_winner",
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    assert snapshot is not None
    assert snapshot.source == "theoddsapi"
    assert snapshot.market_key == "basketball_nba_championship_winner"
    probs = snapshot.fair_probabilities
    assert set(probs.keys()) == {"Boston Celtics", "Denver Nuggets", "Other"}
    total = sum(probs.values())
    # De-vig 后必须严格归一到 1（Decimal 比对要容忍少量浮点误差）。
    assert abs(total - Decimal(1)) < Decimal("0.0000001")
    # raw prob from price=3.x is roughly 0.32 (boston); after devig should
    # be in a sane range.
    assert Decimal("0.20") < probs["Boston Celtics"] < Decimal("0.45")


def test_returns_none_when_market_not_found() -> None:
    payload = [{"id": "other-market", "bookmakers": []}]
    assert (
        parse_theoddsapi_outrights_payload(
            payload, market_key="nba-championship", observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc)
        )
        is None
    )


def test_returns_none_for_non_sequence_payload() -> None:
    assert parse_theoddsapi_outrights_payload({"data": "wrong shape"}, market_key="x") is None
