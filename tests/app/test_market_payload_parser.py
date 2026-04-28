from __future__ import annotations

from polymarket_trader.app.market_payload_parser import MarketPayloadParser


def test_market_payload_parser_preserves_polymarket_game_start_time() -> None:
    """市场解析必须保留真实开赛时间，供直播匹配防串场。"""

    result = MarketPayloadParser().parse(
        {
            "conditionId": "condition-1",
            "slug": "mlb-mia-lad-2026-04-28",
            "question": "Marlins vs Dodgers",
            "clobTokenIds": ["mia", "lad"],
            "outcomes": ["Miami Marlins", "Los Angeles Dodgers"],
            "tickSize": "0.01",
            "orderMinSize": "5",
            "category": "Sports",
            "tags": ["MLB"],
            "endDate": "2026-05-06T02:10:00Z",
            "gameStartTime": "2026-04-29 02:10:00+00",
        }
    )

    market = result.to_market()

    assert market.game_start_time is not None
    assert market.game_start_time.isoformat() == "2026-04-29T02:10:00+00:00"
    assert result.to_event(trace_id="trace-1", event_id="event-1").payload[
        "game_start_time"
    ].isoformat() == "2026-04-29T02:10:00+00:00"
