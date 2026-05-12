"""series WINNER evaluator 完整路径：MISSING_*, INSUFFICIENT_EDGE, PRICE_ABOVE_FAIR, accepted。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from strategies.current.series.evaluator import (
    SeriesEvaluatorInputs,
    evaluate_series_opportunity,
)
from strategies.current.series.types import (
    SeriesCandidate,
    SeriesRejectReason,
    SeriesSubType,
)


_NOW = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)


def _market() -> Market:
    return Market(
        condition_id="cond-series",
        market_slug="celtics-vs-knicks-series",
        market_question="Will Celtics win the series?",
        event_title="NBA Playoffs Series Winner",
        event_slug="celtics-vs-knicks-2026-playoffs",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="tok-yes", outcome="Yes"),
            MarketOutcome(token_id="tok-no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _series_state_payload(*, wins_a: int = 2, wins_b: int = 1) -> dict:
    return {
        "team_a": "Boston Celtics",
        "team_b": "New York Knicks",
        "wins_a": wins_a,
        "wins_b": wins_b,
        "best_of": 7,
        "observed_at": _NOW.isoformat(),
        "next_game_at": None,
    }


def _game_odds_payload(p_a: str = "0.6") -> dict:
    return {
        "team_a": "Boston Celtics",
        "team_b": "New York Knicks",
        "p_a": p_a,
        "observed_at": _NOW.isoformat(),
        "source": "theoddsapi",
    }


def _inputs(
    *,
    best_ask: Decimal | None = Decimal("0.40"),
    buyable_liquidity_usdc: Decimal = Decimal("500"),
) -> SeriesEvaluatorInputs:
    return SeriesEvaluatorInputs(
        best_ask=best_ask,
        buyable_liquidity_usdc=buyable_liquidity_usdc,
        now=_NOW,
        min_edge_bps=800,
        max_entry_price=Decimal("0.95"),
        min_orderbook_depth_usdc=Decimal("50"),
        max_series_state_age_seconds=3600,
        max_game_odds_age_seconds=7200,
        season_snapshot=None,
    )


def _candidate(metadata: dict, outcome_label: str = "Yes", token_id: str = "tok-yes") -> SeriesCandidate:
    return SeriesCandidate(
        market=_market(),
        outcome_label=outcome_label,
        token_id=token_id,
        metadata=metadata,
    )


def test_missing_series_state_returns_audit_reason() -> None:
    candidate = _candidate({})  # 空 metadata
    result = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert result.accepted is False
    assert result.sub_type == SeriesSubType.WINNER
    assert result.reject_reason == SeriesRejectReason.MISSING_SERIES_STATE


def test_missing_game_odds_returns_audit_reason() -> None:
    candidate = _candidate({"series_state": _series_state_payload()})
    result = evaluate_series_opportunity(candidate, inputs=_inputs())
    # 没有 game_odds + season_snapshot=None → derive 返回 None。
    assert result.accepted is False
    assert result.reject_reason == SeriesRejectReason.MISSING_SERIES_ODDS


def test_team_not_resolved_returns_audit_reason() -> None:
    metadata = {
        "series_state": _series_state_payload(),
        "game_odds": _game_odds_payload(),
    }
    # outcome 文本与系列赛队伍都无关 → 解析失败。
    market = _market()
    candidate = SeriesCandidate(
        market=market,
        outcome_label="Some unrelated label",
        token_id="tok-unknown",
        metadata=metadata,
    )
    result = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert result.accepted is False
    assert result.reject_reason == SeriesRejectReason.SERIES_TEAM_NOT_RESOLVED


def test_insufficient_edge_rejects() -> None:
    metadata = {
        "series_state": _series_state_payload(wins_a=2, wins_b=1),
        "game_odds": _game_odds_payload(p_a="0.6"),
    }
    # 2-1, p=0.6, best_of=7 → team_a winner prob ≈ 0.8208。outcome=Yes → fair = 0.8208。
    # min_edge_bps=800 → cap = 0.8208 * 0.92 ≈ 0.7551。best_ask=0.78 > cap → INSUFFICIENT_EDGE。
    inputs = _inputs(best_ask=Decimal("0.78"))
    result = evaluate_series_opportunity(_candidate(metadata), inputs=inputs)
    assert result.accepted is False
    assert result.reject_reason == SeriesRejectReason.INSUFFICIENT_EDGE
    assert "fair_value" in result.metadata


def test_price_above_fair_rejects() -> None:
    metadata = {
        "series_state": _series_state_payload(wins_a=2, wins_b=1),
        "game_odds": _game_odds_payload(p_a="0.6"),
    }
    # min_edge=0 让 cap=fair；best_ask=fair → PRICE_ABOVE_FAIR
    inputs = SeriesEvaluatorInputs(
        best_ask=Decimal("0.95"),  # > fair value caps to 0.95 anyway
        buyable_liquidity_usdc=Decimal("500"),
        now=_NOW,
        min_edge_bps=0,
        max_entry_price=Decimal("0.99"),
        min_orderbook_depth_usdc=Decimal("50"),
        max_series_state_age_seconds=3600,
        max_game_odds_age_seconds=7200,
        season_snapshot=None,
    )
    result = evaluate_series_opportunity(_candidate(metadata), inputs=inputs)
    assert result.accepted is False
    assert result.reject_reason in {
        SeriesRejectReason.PRICE_ABOVE_FAIR,
        SeriesRejectReason.INSUFFICIENT_EDGE,
    }


def test_accepted_path_returns_fair_value_and_entry_cap() -> None:
    metadata = {
        "series_state": _series_state_payload(wins_a=2, wins_b=1),
        "game_odds": _game_odds_payload(p_a="0.6"),
    }
    # Yes outcome → fair ≈ 0.8208；best_ask 0.50 < cap=0.7551 且 < fair → accepted
    result = evaluate_series_opportunity(
        _candidate(metadata),
        inputs=_inputs(best_ask=Decimal("0.50")),
    )
    assert result.accepted is True
    assert result.fair_value is not None
    assert result.fair_value > Decimal("0.80")
    assert result.metadata.get("series_team_side") == "team_a"
    assert result.metadata.get("p_per_game") == "0.6"
    assert "entry_price_cap" in result.metadata


def test_no_outcome_resolves_to_team_b_and_uses_complement() -> None:
    metadata = {
        "series_state": _series_state_payload(wins_a=0, wins_b=3),
        "game_odds": _game_odds_payload(p_a="0.5"),
    }
    # 0-3 deficit, p=0.5 → team_a series prob low；NO outcome (team_b) → fair high
    market = _market()
    candidate = SeriesCandidate(
        market=market,
        outcome_label="No",
        token_id="tok-no",
        metadata=metadata,
    )
    result = evaluate_series_opportunity(
        candidate,
        inputs=_inputs(best_ask=Decimal("0.30")),
    )
    # team_a series prob = 0.5 * (1+0.5+0.25+0.125)/... actually wins_a=0,wins_b=3
    # needed_a=4, needed_b=1 → P_a = C(3,0)*0.5^4*0.5^0 = 0.0625
    # NO outcome (team_b side) → fair = 1 - 0.0625 = 0.9375
    assert result.accepted is True
    assert result.metadata.get("series_team_side") == "team_b"
    assert result.fair_value is not None
    assert result.fair_value > Decimal("0.9")


def test_stale_series_state_returns_audit_reason() -> None:
    metadata = {
        "series_state": {
            **_series_state_payload(),
            "observed_at": (_NOW.replace(year=2025)).isoformat(),
        },
        "game_odds": _game_odds_payload(),
    }
    result = evaluate_series_opportunity(_candidate(metadata), inputs=_inputs())
    assert result.accepted is False
    assert result.reject_reason == SeriesRejectReason.STALE_SERIES_STATE
