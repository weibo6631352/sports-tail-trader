from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.runtime.registry import MarketRegistry

from strategies.current.config import CurrentStrategyConfig
from strategies.current.strategy import CurrentStrategy


def test_entry_plan_hot_path_does_not_read_orderbooks_for_entire_registry() -> None:
    registry = MarketRegistry()
    focus_market = _market(index=0)
    registry.upsert(focus_market)
    for index in range(1, 25):
        registry.upsert(_market(index=index))

    read_tokens: list[str] = []

    def orderbook_reader(token_id: str) -> OrderbookSnapshot | None:
        read_tokens.append(token_id)
        if token_id in focus_market.token_ids:
            return _orderbook(token_id=token_id, condition_id=focus_market.condition_id)
        raise AssertionError(f"hot path read non-focus token {token_id}")

    service = TradingDecisionService(
        strategy_id="sports_tail",
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
        registry=registry,
        orderbook_reader=orderbook_reader,
    )

    service.build_entry_plan(
        market=focus_market,
        orderbook=_orderbook(token_id="token-0-over", condition_id=focus_market.condition_id),
        token_id="token-0-over",
        trace_id="trace-hot-path",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        kelly_fraction=Decimal("0.25"),
        kelly_max_position_fraction=Decimal("1"),
        kelly_min_edge=Decimal("0.02"),
        kelly_min_stake_usdc=Decimal("1"),
        kelly_allow_round_up_to_market_min=True,
        kelly_round_up_max_overbet_ratio=Decimal("1"),
        kelly_drawdown_halt_fraction=Decimal("0.5"),
    )

    assert set(read_tokens).issubset(set(focus_market.token_ids))


def _market(*, index: int) -> Market:
    return Market(
        condition_id=f"condition-{index}",
        market_slug=f"nba-hot-path-{index}-total-over-5pt5",
        market_question=f"NBA hot path {index} total over 5.5?",
        event_title=f"NBA Hot Path {index}",
        event_slug=f"nba-hot-path-{index}",
        outcomes=(
            MarketOutcome(token_id=f"token-{index}-over", outcome="Over"),
            MarketOutcome(token_id=f"token-{index}-under", outcome="Under"),
        ),
        tags=("Sports", "NBA"),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _orderbook(*, token_id: str, condition_id: str) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=token_id,
        condition_id=condition_id,
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.50"),
        best_bid_size=Decimal("10"),
        best_ask_size=Decimal("10"),
        bids=(PriceLevel(price=Decimal("0.40"), size=Decimal("10")),),
        asks=(PriceLevel(price=Decimal("0.50"), size=Decimal("10")),),
        received_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )
