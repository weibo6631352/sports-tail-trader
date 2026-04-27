from __future__ import annotations

from polymarket_trader.app.market_payload_parser import MarketParseRejectReason, MarketPayloadParser


def test_market_payload_parser_accepts_market_with_required_trading_fields() -> None:
    parser = MarketPayloadParser()
    result = parser.parse(
        {
            "category": "Sports",
            "eventTitle": "Any event title is acceptable at parser level",
            "question": "Any market question is acceptable at parser level",
            "slug": "sample-market-a",
            "conditionId": "condition",
            "clobTokenIds": ["yes", "no"],
            "orderPriceMinTickSize": "0.01",
            "orderMinSize": "1",
        }
    )

    assert result.accepted
    event = result.to_event(trace_id="trace", event_id="event")
    assert event.payload["parse_status"] == "accepted"
    assert event.payload["parse_reason"] is None
    assert event.payload["parse_detail"] is None


def test_market_payload_parser_preserves_generic_text_fields() -> None:
    result = MarketPayloadParser().parse(
        {
            "category": "Crypto",
            "eventTitle": "Will this market reach a threshold?",
            "question": "Will this market hit a threshold?",
            "name": "Threshold market",
            "slug": "sample-threshold-market",
            "conditionId": "condition",
            "clobTokenIds": "[\"yes\",\"no\"]",
            "orderPriceMinTickSize": "0.01",
            "orderMinSize": "1",
        }
    )

    assert result.accepted
    market = result.to_market()
    assert market.event_title == "Will this market reach a threshold?"
    assert market.market_question == "Will this market hit a threshold?"
    assert market.market_name == "Threshold market"


def test_market_payload_parser_prefers_fee_schedule_rate_over_taker_base_fee() -> None:
    result = MarketPayloadParser().parse(
        {
            "slug": "sample-market-fees",
            "conditionId": "condition",
            "clobTokenIds": ["yes", "no"],
            "orderPriceMinTickSize": "0.01",
            "orderMinSize": "1",
            "takerBaseFee": 1000,
            "feeSchedule": {
                "rate": 0.072,
                "takerOnly": True,
            },
        }
    )

    assert result.accepted
    assert result.taker_base_fee_bps == 72

    market = result.to_market()
    assert market.taker_base_fee_bps == 72
    assert market.fee_rate_bps == 72


def test_market_payload_parser_rejects_missing_required_identifiers() -> None:
    result = MarketPayloadParser().parse(
        {
            "category": "Sports",
            "eventTitle": "Title",
            "question": "Question",
            "slug": "sample-market-a",
            "conditionId": "condition",
            "clobTokenIds": ["no"],
            "orderPriceMinTickSize": "0.01",
            "orderMinSize": "1",
        }
    )

    assert result.accepted
    assert tuple(outcome.token_id for outcome in result.outcomes) == ("no",)


def test_market_payload_parser_rejects_missing_tick_and_min_order_size() -> None:
    result = MarketPayloadParser().parse(
        {
            "category": "Crypto",
            "eventTitle": "Title",
            "question": "Question",
            "slug": "token-market",
            "conditionId": "condition",
            "clobTokenIds": ["yes", "no"],
        }
    )

    assert not result.accepted
    assert result.reject_reason == MarketParseRejectReason.MISSING_TRADING_CONDITIONS
    event = result.to_event(trace_id="trace", event_id="event")
    assert event.payload["parse_status"] == "rejected"
    assert event.payload["parse_reason"] == MarketParseRejectReason.MISSING_TRADING_CONDITIONS.value
    assert event.payload["parse_detail"] == "missing tick_size / min_order_size"
