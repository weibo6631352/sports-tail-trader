from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.app.market_tracking_policy import market_unsubscribe_prune_reason

# 固定 "now"，prune policy 不依赖墙钟漂移，仅需稳定的参考时间。
_FIXED_NOW = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.position import Position


def test_prunes_paused_filtered_market_without_exposure() -> None:
    market = _market().with_trading_status(
        TradingStatus.PAUSED,
        reject_reason="esports_market_not_auto_tradable",
    )

    assert (
        market_unsubscribe_prune_reason(
            AccountSnapshot(),
            market,
            now=_FIXED_NOW,
        )
        == "esports_market_not_auto_tradable"
    )


def test_keeps_paused_filtered_market_with_exposure() -> None:
    market = _market().with_trading_status(
        TradingStatus.PAUSED,
        reject_reason="series_market_not_auto_tradable",
    )
    account = AccountSnapshot(
        positions=(
            Position(
                strategy_id="sports_tail",
                condition_id=market.condition_id,
                token_id="token-1-yes",
                shares=1,
                cost_usdc=1,
            ),
        )
    )

    assert (
        market_unsubscribe_prune_reason(
            account,
            market,
            now=_FIXED_NOW,
        )
        is None
    )


def _market() -> Market:
    return Market(
        condition_id="condition-1",
        market_slug="market-1",
        outcomes=(
            MarketOutcome(token_id="token-1-yes", outcome="YES"),
            MarketOutcome(token_id="token-1-no", outcome="NO"),
        ),
    )
