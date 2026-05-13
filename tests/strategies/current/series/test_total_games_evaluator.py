"""evaluate_series_opportunity 在 TOTAL_GAMES 子类型上的端到端契约。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from strategies.current.series import (
    SeriesCandidate,
    SeriesEvaluatorInputs,
    SeriesRejectReason,
    SeriesSubType,
    evaluate_series_opportunity,
)


_NOW = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)


def _market(question: str = "Total Games O/U 5.5", outcomes: tuple[tuple[str, str], ...] = (("tok-over", "Over 5.5"),)) -> Market:
    return Market(
        condition_id="cond-totalgames",
        market_slug="series-total-games-ou",
        market_question=question,
        event_title="NBA Series Total Games",
        category="Sports",
        tags=("NBA",),
        outcomes=tuple(MarketOutcome(token_id=tid, outcome=label) for tid, label in outcomes),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _metadata(*, wins_a: int = 0, wins_b: int = 0, p_a: str = "0.5") -> dict:
    return {
        "series_state": {
            "team_a": "Boston Celtics",
            "team_b": "New York Knicks",
            "wins_a": wins_a,
            "wins_b": wins_b,
            "best_of": 7,
            "observed_at": _NOW.isoformat(),
            "next_game_at": None,
        },
        "game_odds": {
            "team_a": "Boston Celtics",
            "team_b": "New York Knicks",
            "p_a": p_a,
            "observed_at": _NOW.isoformat(),
            "source": "theoddsapi",
        },
    }


def _candidate(outcome_label: str = "Over 5.5", *, market: Market | None = None, metadata: dict | None = None) -> SeriesCandidate:
    return SeriesCandidate(
        market=market or _market(),
        outcome_label=outcome_label,
        token_id="tok-over",
        metadata=metadata or _metadata(),
    )


def _inputs(*, best_ask: Decimal | None = Decimal("0.40")) -> SeriesEvaluatorInputs:
    return SeriesEvaluatorInputs(
        best_ask=best_ask,
        buyable_liquidity_usdc=Decimal("100"),
        now=_NOW,
        min_edge_bps=500,
        max_entry_price=Decimal("0.99"),
        min_orderbook_depth_usdc=Decimal("20"),
        max_series_state_age_seconds=3600,
        max_game_odds_age_seconds=7200,
        season_snapshot=None,
    )


def test_total_games_outcome_not_parsed() -> None:
    market = _market(question="Total Games unknown", outcomes=(("tok", "Maybe"),))
    candidate = SeriesCandidate(market=market, outcome_label="Maybe", token_id="tok", metadata=_metadata())
    ev = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert ev.accepted is False
    assert ev.sub_type == SeriesSubType.TOTAL_GAMES
    assert ev.reject_reason == SeriesRejectReason.SERIES_OUTCOME_NOT_PARSED


def test_total_games_accepted_when_over_priced_below_fair() -> None:
    # 0-0 p=0.5 best_of=7 → P(K>=6) ≈ 0.5+ → Over 5.5 fair ≈ 0.5。best_ask=0.40 → 边际充分。
    ev = evaluate_series_opportunity(_candidate("Over 5.5"), inputs=_inputs())
    assert ev.accepted is True
    assert ev.sub_type == SeriesSubType.TOTAL_GAMES
    assert ev.fair_value is not None
    assert ev.fair_value > Decimal("0.40")
    assert ev.metadata["total_games_direction"] == "over"
    assert ev.metadata["total_games_line"] == "5.5"


def test_total_games_under_with_high_ask_rejects_on_edge() -> None:
    # Under 5.5 fair ≈ 0.5；best_ask=0.95 → edge 不足 / price > fair。
    ev = evaluate_series_opportunity(
        _candidate("Under 5.5"),
        inputs=_inputs(best_ask=Decimal("0.95")),
    )
    assert ev.accepted is False
    assert ev.sub_type == SeriesSubType.TOTAL_GAMES
    # 既可能 INSUFFICIENT_EDGE 也可能 PRICE_ABOVE_FAIR，取决于 cap。
    assert ev.reject_reason in (
        SeriesRejectReason.INSUFFICIENT_EDGE,
        SeriesRejectReason.PRICE_ABOVE_FAIR,
    )


def test_total_games_missing_series_state() -> None:
    candidate = SeriesCandidate(
        market=_market(),
        outcome_label="Over 5.5",
        token_id="tok",
        metadata={},  # 无 series_state
    )
    ev = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert ev.accepted is False
    assert ev.reject_reason == SeriesRejectReason.MISSING_SERIES_STATE


def test_total_games_missing_game_odds() -> None:
    metadata = _metadata()
    del metadata["game_odds"]
    candidate = SeriesCandidate(
        market=_market(),
        outcome_label="Over 5.5",
        token_id="tok",
        metadata=metadata,
    )
    ev = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert ev.accepted is False
    assert ev.reject_reason == SeriesRejectReason.MISSING_SERIES_ODDS
