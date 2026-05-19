from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trader.app.market_tracking_policy import market_end_date_elapsed, market_unsubscribe_prune_reason
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.position import Position

# 固定 "now"，prune policy 不依赖墙钟漂移，仅需稳定的参考时间。
_FIXED_NOW = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)


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
        reject_reason="series_market_pending_model",
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


def test_market_end_date_elapsed_within_grace_period() -> None:
    # end_date 刚过 (1h ago) → 仍在 6h 宽限期内 → NOT elapsed
    market = _market(end_date=_FIXED_NOW - timedelta(hours=1))
    assert market_end_date_elapsed(market, now=_FIXED_NOW) is False


def test_market_end_date_elapsed_outside_grace_period() -> None:
    # end_date 超过 6h → elapsed
    market = _market(end_date=_FIXED_NOW - timedelta(hours=7))
    assert market_end_date_elapsed(market, now=_FIXED_NOW) is True


def test_market_not_pruned_within_grace_period() -> None:
    # sports 市场 end_date = game_start_time，赛事开始后 5h 内不应被剔除
    market = _market(end_date=_FIXED_NOW - timedelta(hours=5))
    reason = market_unsubscribe_prune_reason(AccountSnapshot(), market, now=_FIXED_NOW)
    assert reason is None


def _market(end_date: datetime | None = None) -> Market:
    return Market(
        condition_id="condition-1",
        market_slug="market-1",
        end_date=end_date,
        outcomes=(
            MarketOutcome(token_id="token-1-yes", outcome="YES"),
            MarketOutcome(token_id="token-1-no", outcome="NO"),
        ),
    )
