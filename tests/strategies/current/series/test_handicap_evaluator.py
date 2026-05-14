"""evaluate_series_opportunity 在 GAME_HANDICAP 子类型上的端到端契约。"""

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


def _market(
    question: str,
    outcomes: tuple[tuple[str, str], ...] = (("tok", "Celtics -1.5"),),
) -> Market:
    return Market(
        condition_id="cond-handicap",
        market_slug="celtics-knicks-handicap",
        market_question=question,
        event_title="NBA Series Handicap",
        category="Sports",
        tags=("NBA",),
        outcomes=tuple(MarketOutcome(token_id=tid, outcome=label) for tid, label in outcomes),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _metadata(
    *,
    include_game_odds: bool = True,
    include_spreads: bool = False,
    spread_line: str = "-3.5",
    p_a_covers: str = "0.55",
) -> dict:
    meta: dict = {
        "series_state": {
            "team_a": "Boston Celtics",
            "team_b": "New York Knicks",
            "wins_a": 1,
            "wins_b": 1,
            "best_of": 7,
            "observed_at": _NOW.isoformat(),
            "next_game_at": None,
        },
    }
    if include_game_odds:
        meta["game_odds"] = {
            "team_a": "Boston Celtics",
            "team_b": "New York Knicks",
            "p_a": "0.55",
            "observed_at": _NOW.isoformat(),
            "source": "theoddsapi",
        }
    if include_spreads:
        meta["game_spreads"] = {
            "team_a": "Boston Celtics",
            "team_b": "New York Knicks",
            "spread_line": spread_line,
            "p_a_covers": p_a_covers,
            "observed_at": _NOW.isoformat(),
            "source": "theoddsapi",
        }
    return meta


def _inputs(*, best_ask: Decimal | None = Decimal("0.40")) -> SeriesEvaluatorInputs:
    return SeriesEvaluatorInputs(
        best_ask=best_ask,
        best_bid=None,
        buyable_liquidity_usdc=Decimal("100"),
        now=_NOW,
        min_edge_bps=500,
        max_entry_price=Decimal("0.99"),
        min_orderbook_depth_usdc=Decimal("20"),
        max_series_state_age_seconds=3600,
        max_game_odds_age_seconds=7200,
        season_snapshot=None,
    )


def test_series_scope_handicap_accepted() -> None:
    # series scope：Boston -1.5 in the series → 走 series_handicap_cover_probability
    market = _market("Boston Celtics series handicap -1.5", outcomes=(("tok", "Celtics -1.5"),))
    candidate = SeriesCandidate(
        market=market,
        outcome_label="Celtics -1.5",
        token_id="tok",
        metadata=_metadata(),
    )
    ev = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert ev.sub_type == SeriesSubType.GAME_HANDICAP
    # accepted 与否取决于 fair vs best_ask；fair_value 必须算出
    assert ev.fair_value is not None
    assert ev.metadata["handicap_scope"] == "series"


def test_single_game_scope_requires_spreads() -> None:
    # outcome 指向 Game 5 → single_game scope；缺 spreads → MISSING_GAME_SPREADS
    market = _market("Boston Celtics -3.5 in Game 5", outcomes=(("tok", "Celtics -3.5"),))
    candidate = SeriesCandidate(
        market=market,
        outcome_label="Celtics -3.5",
        token_id="tok",
        metadata=_metadata(include_spreads=False),
    )
    ev = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert ev.accepted is False
    assert ev.sub_type == SeriesSubType.GAME_HANDICAP
    assert ev.reject_reason == SeriesRejectReason.MISSING_GAME_SPREADS


def test_single_game_scope_accepted_when_spreads_match() -> None:
    market = _market("Boston Celtics -3.5 in Game 5", outcomes=(("tok", "Celtics -3.5"),))
    candidate = SeriesCandidate(
        market=market,
        outcome_label="Celtics -3.5",
        token_id="tok",
        metadata=_metadata(include_spreads=True, spread_line="-3.5", p_a_covers="0.55"),
    )
    ev = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert ev.sub_type == SeriesSubType.GAME_HANDICAP
    assert ev.fair_value is not None
    assert ev.metadata["handicap_scope"] == "single_game"
    assert ev.fair_value == Decimal("0.55")


def test_handicap_outcome_not_parsed() -> None:
    market = _market("Series handicap mystery", outcomes=(("tok", "Maybe"),))
    candidate = SeriesCandidate(
        market=market,
        outcome_label="Maybe",
        token_id="tok",
        metadata=_metadata(),
    )
    ev = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert ev.accepted is False
    assert ev.sub_type == SeriesSubType.GAME_HANDICAP
    assert ev.reject_reason == SeriesRejectReason.SERIES_OUTCOME_NOT_PARSED


def test_single_game_scope_missing_state() -> None:
    market = _market("Boston Celtics -3.5 in Game 5", outcomes=(("tok", "Celtics -3.5"),))
    candidate = SeriesCandidate(
        market=market,
        outcome_label="Celtics -3.5",
        token_id="tok",
        metadata={},  # 没 series_state
    )
    ev = evaluate_series_opportunity(candidate, inputs=_inputs())
    assert ev.accepted is False
    assert ev.reject_reason == SeriesRejectReason.MISSING_SERIES_STATE
