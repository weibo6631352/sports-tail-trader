from __future__ import annotations

import json
from pathlib import Path

from polymarket_trader.app.extension_host import run_entry_replay


def test_entry_replay_fixture_metadata_drives_sports_tail_plan(tmp_path: Path) -> None:
    fixture_path = tmp_path / "sports-tail-replay.json"
    fixture_path.write_text(
        json.dumps(
            {
                "trace_id": "trace-replay",
                "markets": [
                    {
                        "condition_id": "totals-condition",
                        "market_slug": "nhl-tb-mon-total-4-5",
                        "market_question": "TB vs MON total over/under 4.5",
                        "event_title": "TB vs MON",
                        "category": "Sports",
                        "tags": ["NHL"],
                        "trading_status": "eligible",
                        "outcomes": [
                            {"token_id": "over", "outcome": "Over"},
                            {"token_id": "under", "outcome": "Under"},
                        ],
                    }
                ],
                "orderbooks": [
                    {
                        "token_id": "over",
                        "condition_id": "totals-condition",
                        "market_slug": "nhl-tb-mon-total-4-5",
                        "best_bid": "0.97",
                        "best_ask": "0.98",
                        "bids": [{"price": "0.97", "size": "20"}],
                        "asks": [{"price": "0.98", "size": "20"}],
                        "received_at": "2026-04-27T00:00:00+00:00",
                    }
                ],
                "budgets": {
                    "portfolio_budget_usdc": "10",
                    "available_usdc": "10",
                    "max_order_usdc": "10",
                    "max_market_usdc": "10",
                    "max_total_usdc": "10",
                },
                "target": {
                    "condition_id": "totals-condition",
                    "token_id": "over",
                },
                "metadata": {
                    "sports_tail_game": {
                        "league": "NHL",
                        "home_name": "TB",
                        "away_name": "MON",
                        "home_score": 3,
                        "away_score": 2,
                        "period": "P3",
                        "seconds_remaining": 420,
                        "status": "live",
                        "observed_at": "2026-04-27T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = run_entry_replay(str(fixture_path), extension_module="strategies.current")

    assert result["plan"]["ready_to_trade"] is True
    assert result["plan"]["metadata"]["sports_tail_reason"] == "totals_over_locked"
    assert result["plan"]["metadata"]["sports_execution_permission"] == "auto_execute"
