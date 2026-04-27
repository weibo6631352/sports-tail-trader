from __future__ import annotations

from polymarket_trader.app.trade_replay_validation import validate_trade_replays_against_positions


def test_trade_replay_validation_compares_replay_with_polymarket_position_export() -> None:
    trade_replays = {
        "items": [
            {
                "condition_id": "condition-1",
                "token_id": "token-1",
                "position": {
                    "shares": "5",
                    "cost_usdc": "4",
                },
                "pnl": {
                    "realized_pnl_usdc": "0.75",
                    "cash_pnl_usdc": "1.65",
                    "current_value_usdc": "4.90",
                    "cur_price": "0.98",
                    "redeemable": True,
                },
            }
        ]
    }
    positions = {
        "positions": [
            {
                "conditionId": "condition-1",
                "asset": "token-1",
                "shares": "5",
                "cost": "4",
                "realizedPnl": "0.75",
                "cashPnl": "1.65",
                "currentValue": "4.90",
                "curPrice": "0.98",
                "redeemable": True,
            }
        ]
    }

    report = validate_trade_replays_against_positions(trade_replays, positions)

    assert report.passed is True
    assert report.checked_records == 1
    assert report.matched_positions == 1


def test_trade_replay_validation_reports_mismatch() -> None:
    trade_replays = {
        "items": [
            {
                "condition_id": "condition-1",
                "token_id": "token-1",
                "position": {"shares": "5", "cost_usdc": "4"},
                "pnl": {"cash_pnl_usdc": "1.20"},
            }
        ]
    }
    positions = {
        "positions": [
            {
                "conditionId": "condition-1",
                "asset": "token-1",
                "shares": "5",
                "cost": "4",
                "cashPnl": "1.65",
            }
        ]
    }

    report = validate_trade_replays_against_positions(trade_replays, positions)

    assert report.passed is False
    assert report.mismatches[0].field == "pnl.cash_pnl_usdc"


def test_trade_replay_validation_accepts_tuple_payload_items() -> None:
    trade_replays = {
        "items": (
            {
                "condition_id": "condition-1",
                "token_id": "token-1",
                "position": {"shares": "2", "cost_usdc": "1.5"},
                "pnl": {"cur_price": "0.75"},
            },
        )
    }
    positions = {
        "positions": (
            {
                "conditionId": "condition-1",
                "asset": "token-1",
                "shares": "2",
                "cost": "1.5",
                "curPrice": "0.75",
            },
        )
    }

    report = validate_trade_replays_against_positions(trade_replays, positions)

    assert report.passed is True
    assert report.checked_records == 1
    assert report.matched_positions == 1
