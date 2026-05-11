from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from polymarket_trader.domain.position_lifecycle import (
    PositionLifecycleStage,
    classify,
)


def test_entry_pending_when_no_shares_with_open_buy() -> None:
    stage = classify(
        shares=Decimal("0"),
        open_buy_shares=Decimal("100"),
        open_sell_shares=Decimal("0"),
        confirmed_shares=Decimal("0"),
    )
    assert stage is PositionLifecycleStage.ENTRY_PENDING
    assert stage.value == "entry_pending"


def test_holding_when_shares_without_open_sell() -> None:
    stage = classify(
        shares=Decimal("50"),
        open_buy_shares=Decimal("0"),
        open_sell_shares=Decimal("0"),
        confirmed_shares=Decimal("50"),
    )
    assert stage is PositionLifecycleStage.HOLDING


def test_exiting_when_shares_and_open_sell() -> None:
    stage = classify(
        shares=Decimal("50"),
        open_buy_shares=Decimal("0"),
        open_sell_shares=Decimal("25"),
        confirmed_shares=Decimal("50"),
    )
    assert stage is PositionLifecycleStage.EXITING


def test_closed_when_shares_zero_and_confirmed_history() -> None:
    stage = classify(
        shares=Decimal("0"),
        open_buy_shares=Decimal("0"),
        open_sell_shares=Decimal("0"),
        confirmed_shares=Decimal("75"),
    )
    assert stage is PositionLifecycleStage.CLOSED


def test_unknown_with_warn_log_when_no_history_and_no_open_orders(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="polymarket_trader.domain.position_lifecycle")
    stage = classify(
        shares=Decimal("0"),
        open_buy_shares=Decimal("0"),
        open_sell_shares=Decimal("0"),
        confirmed_shares=Decimal("0"),
    )
    assert stage is PositionLifecycleStage.UNKNOWN
    warn_records = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "polymarket_trader.domain.position_lifecycle"
    ]
    assert warn_records, "UNKNOWN classification must emit a WARN log"
    assert "position lifecycle UNKNOWN" in warn_records[0].getMessage()
