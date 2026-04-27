from __future__ import annotations

from strategies.current.calibration import run_sports_tail_calibration


def test_calibration_reports_moneyline_and_spreads_threshold_effects() -> None:
    sample = {
        "rule_version": "test",
        "now": "2026-04-27T00:00:00+00:00",
        "policy_variants": [
            {
                "name": "base",
                "overrides": {
                    "max_moneyline_seconds_remaining": 180,
                    "min_moneyline_lead": 6,
                    "max_spreads_seconds_remaining": 120,
                    "min_spread_safety_margin": 2,
                },
            },
            {
                "name": "stricter",
                "overrides": {
                    "max_moneyline_seconds_remaining": 60,
                    "min_moneyline_lead": 10,
                    "max_spreads_seconds_remaining": 60,
                    "min_spread_safety_margin": 5,
                },
            },
        ],
        "cases": [
            {
                "case_id": "moneyline-late-lead",
                "game": {
                    "league": "NBA",
                    "home_name": "Knicks",
                    "away_name": "Celtics",
                    "home_score": 102,
                    "away_score": 94,
                    "period": "Q4",
                    "seconds_remaining": 90,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                },
                "market": {
                    "market_type": "moneyline",
                    "side": "home",
                    "token_id": "home",
                    "best_ask": "0.95",
                    "buyable_liquidity_usdc": "20",
                },
                "label": {"should_accept": True, "pnl_usdc": "0.25"},
            },
            {
                "case_id": "spreads-cover",
                "game": {
                    "league": "NBA",
                    "home_name": "Knicks",
                    "away_name": "Celtics",
                    "home_score": 102,
                    "away_score": 98,
                    "period": "Q4",
                    "seconds_remaining": 75,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                },
                "market": {
                    "market_type": "spreads",
                    "side": "home",
                    "line": "-1.5",
                    "token_id": "home-spread",
                    "best_ask": "0.94",
                    "buyable_liquidity_usdc": "20",
                },
                "label": {"should_accept": True, "pnl_usdc": "0.10"},
            },
        ],
    }

    report = run_sports_tail_calibration(sample).as_payload()
    variants = {variant["name"]: variant for variant in report["variants"]}

    assert variants["base"]["accepted_count"] == 2
    assert variants["base"]["by_market_type"]["moneyline"]["true_positive"] == 1
    assert variants["stricter"]["accepted_count"] == 0
    assert variants["stricter"]["reason_counts"]["game_not_late_enough"] == 2
