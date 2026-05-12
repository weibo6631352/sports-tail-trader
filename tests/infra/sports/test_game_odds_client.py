"""TheOddsAPI h2h payload → GameOddsSnapshot 解析。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.infra.sports.game_odds_client import (
    parse_theoddsapi_h2h_payload,
)


_OBS = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def _payload(home: str = "Boston Celtics", away: str = "New York Knicks", price_home: float = 1.6, price_away: float = 2.5, event_id: str = "ev-1") -> list[dict]:
    return [
        {
            "id": event_id,
            "sport_key": "basketball_nba",
            "home_team": home,
            "away_team": away,
            "commence_time": "2026-05-15T23:30:00Z",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": home, "price": price_home},
                                {"name": away, "price": price_away},
                            ],
                        }
                    ],
                },
                {
                    "key": "fanduel",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": home, "price": price_home * 1.02},
                                {"name": away, "price": price_away * 0.98},
                            ],
                        }
                    ],
                },
            ],
        }
    ]


def test_parses_h2h_payload_with_devig() -> None:
    payload = _payload()
    snapshot = parse_theoddsapi_h2h_payload(
        payload,
        game_key="ev-1",
        observed_at=_OBS,
    )
    assert snapshot is not None
    assert snapshot.team_a == "Boston Celtics"
    assert snapshot.team_b == "New York Knicks"
    # raw p_home ~ 1/1.6 = 0.625; raw p_away ~ 1/2.5 = 0.4; sum 1.025 → de-vig 0.625/1.025 ≈ 0.61
    assert Decimal("0.55") < snapshot.p_a < Decimal("0.7")
    assert snapshot.source == "theoddsapi"


def test_matches_on_team_label_fallback() -> None:
    payload = _payload(event_id="ev-1")
    snapshot = parse_theoddsapi_h2h_payload(
        payload,
        game_key="Boston Celtics New York Knicks",
        observed_at=_OBS,
    )
    assert snapshot is not None


def test_returns_none_when_event_missing() -> None:
    assert (
        parse_theoddsapi_h2h_payload(
            _payload(),
            game_key="unknown-event-id",
            observed_at=_OBS,
        )
        is None
    )


def test_returns_none_for_invalid_payload_shape() -> None:
    assert parse_theoddsapi_h2h_payload({"oops": True}, game_key="x") is None


def test_returns_none_when_h2h_market_missing() -> None:
    payload = _payload()
    payload[0]["bookmakers"][0]["markets"][0]["key"] = "spreads"
    payload[0]["bookmakers"][1]["markets"][0]["key"] = "spreads"
    snapshot = parse_theoddsapi_h2h_payload(payload, game_key="ev-1", observed_at=_OBS)
    assert snapshot is None


def test_returns_none_when_outcome_pricing_invalid() -> None:
    payload = _payload()
    payload[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 0
    payload[0]["bookmakers"][1]["markets"][0]["outcomes"][0]["price"] = -1
    snapshot = parse_theoddsapi_h2h_payload(payload, game_key="ev-1", observed_at=_OBS)
    # 两个 bookmaker 的 home 都被丢弃 → home outcome 没有概率 → 返回 None
    assert snapshot is None


def test_p_a_corresponds_to_home_team() -> None:
    # price_home 低于 price_away → home_team 是更高概率赢方
    payload = _payload(price_home=1.2, price_away=5.0)
    snapshot = parse_theoddsapi_h2h_payload(payload, game_key="ev-1", observed_at=_OBS)
    assert snapshot is not None
    assert snapshot.p_a > Decimal("0.7")
