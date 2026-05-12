"""Strategy-level kelly_plan integration tests.

domain/kelly.py 已经覆盖单市场 Kelly 公式的全部边界。这里专门测策略侧的：
- 基本流程（接受/拒绝市场）
- §1 sequential bankroll：多市场分配按 f_star 排序、bankroll 扣减
- prob_provider 返回 None 时跳过
- strategy_budget_cap_usdc 截断
- market_min_order_size 凑齐路径
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from strategies.current.allocation import (
    AllocationMarketSnapshot,
    ProbView,
    kelly_plan,
)

_FIXED_NOW = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)


def _snapshot(
    *,
    condition_id: str = "condition",
    market_slug: str = "market",
    token_id: str = "yes",
    best_ask: Decimal,
    min_order_size: Decimal,
    best_ask_size: Decimal = Decimal("100"),
    fee_rate_bps: int | None = None,
    strategy_budget_cap_usdc: Decimal | None = None,
) -> AllocationMarketSnapshot:
    orderbook = OrderbookSnapshot(
        token_id=token_id,
        best_bid=best_ask - Decimal("0.01"),
        best_ask=best_ask,
        bids=(PriceLevel(price=best_ask - Decimal("0.01"), size=Decimal("100")),),
        asks=(PriceLevel(price=best_ask, size=best_ask_size),),
        received_at=_FIXED_NOW,
        condition_id=condition_id,
        market_slug=market_slug,
        best_bid_size=Decimal("100"),
        best_ask_size=best_ask_size,
        tick_size=Decimal("0.01"),
    )
    return AllocationMarketSnapshot(
        market=Market(
            condition_id=condition_id,
            market_slug=market_slug,
            outcomes=(
                MarketOutcome(token_id="yes", outcome="Yes"),
                MarketOutcome(token_id="no", outcome="No"),
            ),
            trading_status=TradingStatus.ELIGIBLE,
            min_order_size=min_order_size,
        ).with_fee_rate(fee_rate_bps),
        token_id=token_id,
        orderbook=orderbook,
        best_ask=best_ask,
        best_ask_size=best_ask_size,
        strategy_budget_cap_usdc=strategy_budget_cap_usdc,
    )


def _kelly_kwargs(**overrides):
    base = dict(
        kelly_fraction=Decimal("0.25"),
        kelly_max_position_fraction=Decimal("0.10"),
        kelly_min_edge=Decimal("0.02"),
        kelly_min_stake_usdc=Decimal("1"),
        kelly_allow_round_up_to_market_min=True,
        kelly_round_up_max_overbet_ratio=Decimal("1"),
    )
    base.update(overrides)
    return base


def _const_prob(prob_p: Decimal | None, confidence: Decimal = Decimal("1")):
    return lambda _snap: ProbView(prob_p=prob_p, prob_confidence=confidence, source="test")


def test_kelly_plan_accepts_market_with_positive_edge():
    plan = kelly_plan(
        trace_id="trace-basic",
        bankroll_usdc=Decimal("100"),
        portfolio_budget_usdc=Decimal("100"),
        markets=(_snapshot(best_ask=Decimal("0.10"), min_order_size=Decimal("5")),),
        prob_provider=_const_prob(Decimal("0.20")),
        **_kelly_kwargs(),
    )
    assert plan.allocations[0].buy_budget_usdc > Decimal("0")
    assert plan.allocations[0].kelly_f_star is not None
    assert plan.allocations[0].kelly_f_star > Decimal("0")
    assert plan.allocations[0].prob_p == Decimal("0.20")


def test_kelly_plan_skips_when_prob_p_is_none():
    plan = kelly_plan(
        trace_id="trace-no-prob",
        bankroll_usdc=Decimal("100"),
        portfolio_budget_usdc=Decimal("100"),
        markets=(_snapshot(best_ask=Decimal("0.10"), min_order_size=Decimal("5")),),
        prob_provider=_const_prob(None),
        **_kelly_kwargs(),
    )
    allocation = plan.allocations[0]
    assert allocation.buy_budget_usdc == Decimal("0")
    assert allocation.reason == "missing_price_or_prob"


def test_kelly_plan_rejects_market_with_below_min_edge():
    plan = kelly_plan(
        trace_id="trace-below-edge",
        bankroll_usdc=Decimal("100"),
        portfolio_budget_usdc=Decimal("100"),
        markets=(_snapshot(best_ask=Decimal("0.10"), min_order_size=Decimal("5")),),
        prob_provider=_const_prob(Decimal("0.105")),  # edge=0.005 < min_edge=0.02
        **_kelly_kwargs(),
    )
    allocation = plan.allocations[0]
    assert allocation.buy_budget_usdc == Decimal("0")
    assert allocation.reason == "edge_below_min"
    assert allocation.kelly_f_star is not None  # 仍带审计字段


def test_kelly_plan_sequential_bankroll_decrements():
    """3 个 market 同时 size，第 1 笔扣减 bankroll，后续基于 remaining_bankroll 计算。"""

    markets = (
        _snapshot(condition_id="c1", market_slug="m1", best_ask=Decimal("0.10"), min_order_size=Decimal("5")),
        _snapshot(condition_id="c2", market_slug="m2", best_ask=Decimal("0.20"), min_order_size=Decimal("5")),
        _snapshot(condition_id="c3", market_slug="m3", best_ask=Decimal("0.30"), min_order_size=Decimal("5")),
    )

    def _per_market_prob(snap):
        # c1 给最高 edge → c1 应该先 size
        if snap.condition_id == "c1":
            return ProbView(prob_p=Decimal("0.50"), prob_confidence=Decimal("1"), source="test")
        if snap.condition_id == "c2":
            return ProbView(prob_p=Decimal("0.40"), prob_confidence=Decimal("1"), source="test")
        return ProbView(prob_p=Decimal("0.50"), prob_confidence=Decimal("1"), source="test")

    plan = kelly_plan(
        trace_id="trace-seq",
        bankroll_usdc=Decimal("20"),
        portfolio_budget_usdc=Decimal("20"),
        markets=markets,
        prob_provider=_per_market_prob,
        **_kelly_kwargs(),
    )
    accepted = [a for a in plan.allocations if a.buy_budget_usdc > Decimal("0")]
    # 总分配不超 bankroll
    total = sum((a.buy_budget_usdc for a in accepted), Decimal("0"))
    assert total <= Decimal("20")
    # 至少有一笔被 sized
    assert accepted, "expected at least one Kelly stake"


def test_kelly_plan_strategy_budget_cap_truncates():
    plan = kelly_plan(
        trace_id="trace-cap",
        bankroll_usdc=Decimal("100"),
        portfolio_budget_usdc=Decimal("100"),
        markets=(
            _snapshot(
                best_ask=Decimal("0.10"),
                min_order_size=Decimal("5"),
                strategy_budget_cap_usdc=Decimal("0.5"),
            ),
        ),
        prob_provider=_const_prob(Decimal("0.50")),
        **_kelly_kwargs(),
    )
    allocation = plan.allocations[0]
    assert allocation.buy_budget_usdc == Decimal("0.5")
    assert allocation.capped_by == "strategy_budget_cap"


def test_kelly_plan_market_min_round_up_overbet_flag():
    """small bankroll → Kelly 推荐 < market min；round-up 后 over_bet=True 入审计。"""

    plan = kelly_plan(
        trace_id="trace-roundup",
        bankroll_usdc=Decimal("100"),
        portfolio_budget_usdc=Decimal("100"),
        markets=(
            _snapshot(best_ask=Decimal("0.10"), min_order_size=Decimal("20")),
        ),
        prob_provider=_const_prob(Decimal("0.13")),  # small edge → small Kelly
        **_kelly_kwargs(),
    )
    allocation = plan.allocations[0]
    # market_min=20 shares × 0.10 = 2 USDC；raw Kelly 远小于此 → 凑齐到 2
    assert allocation.buy_budget_usdc == Decimal("2")
    assert allocation.is_round_up_overbet is True
    assert allocation.capped_by == "rounded_up_to_market_min"


def test_kelly_plan_no_eligible_markets_returns_reason():
    plan = kelly_plan(
        trace_id="trace-empty",
        bankroll_usdc=Decimal("100"),
        portfolio_budget_usdc=Decimal("100"),
        markets=(),
        prob_provider=_const_prob(Decimal("0.20")),
        **_kelly_kwargs(),
    )
    assert plan.reason == "no_eligible_market"
    assert len(plan.allocations) == 0
