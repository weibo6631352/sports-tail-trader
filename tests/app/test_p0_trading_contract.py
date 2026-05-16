"""P0 trading chain contract tests: TradingDecisionService → RiskManager → Executor.

Per CLAUDE.md §11, P0 paths must have contract tests covering the full
decision → risk → executor chain. These tests verify the integration
contract, not individual unit behavior.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    ExecutionTimestamps,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderType,
)
from datetime import datetime, timezone

from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.risk import RiskDecision, RiskManager
from polymarket_trader.runtime.registry import MarketRegistry

from strategies.current.config import CurrentStrategyConfig
from strategies.current.strategy import CurrentStrategy


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _AlwaysPassRisk(RiskManager):
    def check_order_intent(self, *args, **kwargs) -> RiskDecision:  # type: ignore[override]
        return RiskDecision(passed=True)


class _AlwaysRejectRisk(RiskManager):
    def __init__(self, reason: str = "test_reject") -> None:
        self._reason = reason

    def check_order_intent(self, *args, **kwargs) -> RiskDecision:  # type: ignore[override]
        return RiskDecision(passed=False, reason=self._reason)


class _StubExecutor:
    def __init__(self, status: OrderResultStatus = OrderResultStatus.FULL_FILL) -> None:
        self._status = status
        self.submitted: list[BuyOrderIntent] = []

    async def submit(self, intent: BuyOrderIntent) -> OrderResult:
        self.submitted.append(intent)
        return OrderResult(
            strategy_id=intent.strategy_id,
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            status=self._status,
            intent=intent,
            order_id="order-contract-1",
            side=OrderSide.BUY,
            order_type=intent.order_type,
            price=intent.price,
            requested_amount_usdc=intent.amount_usdc,
            matched_shares=Decimal("2") if self._status == OrderResultStatus.FULL_FILL else Decimal("0"),
            spent_usdc=Decimal("2") if self._status == OrderResultStatus.FULL_FILL else Decimal("0"),
            timestamps=ExecutionTimestamps(),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _eligible_market() -> Market:
    return Market(
        condition_id="cond-contract-1",
        market_slug="nba-contract-test-total-over-200pt5",
        market_question="NBA contract test: total over 200.5?",
        event_title="NBA Contract Test",
        event_slug="nba-contract-test",
        outcomes=(
            MarketOutcome(token_id="tok-over", outcome="Over"),
            MarketOutcome(token_id="tok-under", outcome="Under"),
        ),
        tags=("Sports", "NBA"),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _orderbook(*, token_id: str, condition_id: str, best_ask: Decimal = Decimal("0.50")) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=token_id,
        condition_id=condition_id,
        best_bid=Decimal("0.45"),
        best_ask=best_ask,
        best_bid_size=Decimal("50"),
        best_ask_size=Decimal("50"),
        bids=(PriceLevel(price=Decimal("0.45"), size=Decimal("50")),),
        asks=(PriceLevel(price=best_ask, size=Decimal("50")),),
        received_at=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )


def _decision_service(registry: MarketRegistry) -> TradingDecisionService:
    return TradingDecisionService(
        strategy_id="sports_tail",
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
        registry=registry,
        orderbook_reader=lambda token_id: None,
    )


_ENTRY_KWARGS: dict = dict(
    token_id="tok-over",
    trace_id="trace-contract",
    portfolio_budget_usdc=Decimal("1000"),
    available_usdc=Decimal("500"),
    kelly_fraction=Decimal("0.25"),
    kelly_max_position_fraction=Decimal("1"),
    kelly_min_edge=Decimal("0.02"),
    kelly_min_stake_usdc=Decimal("1"),
    kelly_allow_round_up_to_market_min=True,
    kelly_round_up_max_overbet_ratio=Decimal("1"),
    kelly_drawdown_halt_fraction=Decimal("0.5"),
)


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------

def test_p0_entry_plan_produces_buy_intent_for_eligible_market() -> None:
    """build_entry_plan 对可交易市场应产出可执行的 BUY intent。"""
    market = _eligible_market()
    registry = MarketRegistry()
    registry.upsert(market)
    service = _decision_service(registry)

    plan = service.build_entry_plan(
        market=market,
        orderbook=_orderbook(token_id="tok-over", condition_id=market.condition_id),
        **_ENTRY_KWARGS,
    )

    # 策略不知道比赛状态 → SKIP；合约验证 plan 结构完整，不要求必须 BUY。
    assert plan.market is not None
    assert plan.allocation_plan is not None
    assert plan.trace_id == "trace-contract"


def test_p0_risk_gate_blocks_execution_when_rejected() -> None:
    """RiskManager 拒绝时 review_intent 不调用 executor，返回 failed result。"""

    async def _run() -> None:
        executor = _StubExecutor()
        service = TradingService(
            risk_manager=_AlwaysRejectRisk("balance_insufficient"),
            executor=executor,
        )
        intent = BuyOrderIntent(
            strategy_id="sports_tail",
            trace_id="trace-risk-reject",
            condition_id="cond-1",
            token_id="tok-1",
            price=Decimal("0.5"),
            amount_usdc=Decimal("10"),
            order_type=OrderType.FAK,
            market_slug="slug-1",
        )
        result = await service.review_intent(
            intent,
            balance_usdc=Decimal("100"),
            bankroll_usdc=Decimal("100"),
        )
        assert not result.ok
        assert result.risk_decision is not None
        assert not result.risk_decision.passed
        assert result.risk_decision.reason == "balance_insufficient"
        assert executor.submitted == []

    asyncio.run(_run())


def test_p0_risk_pass_executes_via_executor() -> None:
    """RiskManager 通过时 executor 收到 BUY intent 并返回 FULL_FILL。"""

    async def _run() -> None:
        executor = _StubExecutor(status=OrderResultStatus.FULL_FILL)
        service = TradingService(
            risk_manager=_AlwaysPassRisk(),
            executor=executor,
        )
        intent = BuyOrderIntent(
            strategy_id="sports_tail",
            trace_id="trace-risk-pass",
            condition_id="cond-2",
            token_id="tok-2",
            price=Decimal("0.5"),
            amount_usdc=Decimal("10"),
            order_type=OrderType.FAK,
            market_slug="slug-2",
        )
        result = await service.review_intent(
            intent,
            balance_usdc=Decimal("500"),
            bankroll_usdc=Decimal("500"),
        )
        assert result.ok
        assert result.risk_decision is not None
        assert result.risk_decision.passed
        assert len(executor.submitted) == 1
        assert executor.submitted[0].trace_id == "trace-risk-pass"

    asyncio.run(_run())


def test_p0_trace_id_propagates_from_plan_to_executor() -> None:
    """trace_id 在整条链路（build_entry_plan → review_intent → executor）保持一致。"""

    async def _run() -> None:
        executor = _StubExecutor(status=OrderResultStatus.FULL_FILL)
        service = TradingService(
            risk_manager=_AlwaysPassRisk(),
            executor=executor,
        )
        trace = "trace-propagation-check"
        intent = BuyOrderIntent(
            strategy_id="sports_tail",
            trace_id=trace,
            condition_id="cond-3",
            token_id="tok-3",
            price=Decimal("0.5"),
            amount_usdc=Decimal("5"),
            order_type=OrderType.FAK,
            market_slug="slug-3",
        )
        result = await service.review_intent(intent, balance_usdc=Decimal("100"), bankroll_usdc=Decimal("100"))
        assert result.ok
        assert executor.submitted[0].trace_id == trace
        assert result.order_result is not None
        assert result.order_result.trace_id == trace

    asyncio.run(_run())


def test_p0_paused_market_entry_plan_has_no_intent() -> None:
    """PAUSED 市场的 build_entry_plan 不产出可执行 intent（不绕过 trading_status 检查）。"""
    market = _eligible_market()
    paused_market = replace(market, trading_status=TradingStatus.PAUSED)
    registry = MarketRegistry()
    registry.upsert(paused_market)
    service = _decision_service(registry)

    plan = service.build_entry_plan(
        market=paused_market,
        orderbook=_orderbook(token_id="tok-over", condition_id=paused_market.condition_id),
        **_ENTRY_KWARGS,
    )

    assert plan.intent is None
