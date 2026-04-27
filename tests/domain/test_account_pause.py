from __future__ import annotations

from polymarket_trader.domain.account import (
    AccountSnapshot,
    MarketPause,
    MarketPauseReason,
    MarketPauseSource,
)


def test_account_snapshot_uses_structured_market_pauses() -> None:
    pause = MarketPause.build(
        condition_id="condition-1",
        reason=MarketPauseReason.MISSING_PRIMARY_OUTCOME,
        source=MarketPauseSource.RECONCILE,
    )

    snapshot = AccountSnapshot(market_pauses=(pause,))

    assert snapshot.is_market_paused("condition-1")
    assert snapshot.pause_for_market("condition-1") == pause
    assert tuple(item.condition_id for item in snapshot.market_pauses) == ("condition-1",)
    assert tuple(item.as_reason_pair() for item in snapshot.market_pauses) == (
        ("condition-1", "missing_primary_outcome"),
    )
    assert pause.recoverable is True


def test_account_snapshot_removes_recoverable_pause_without_touching_sticky_pause() -> None:
    recoverable = MarketPause.build(
        condition_id="condition-1",
        reason=MarketPauseReason.MARKET_NOT_TRADABLE,
        source=MarketPauseSource.RECONCILE,
    )
    sticky = MarketPause.build(
        condition_id="condition-2",
        reason=MarketPauseReason.UNEXPECTED_RESTING_ORDER,
        source=MarketPauseSource.RISK,
    )
    snapshot = AccountSnapshot(market_pauses=(recoverable, sticky))

    cleared = snapshot.without_market_pause("condition-1")

    assert cleared.pause_for_market("condition-1") is None
    assert cleared.pause_for_market("condition-2") == sticky
    assert sticky.recoverable is False
