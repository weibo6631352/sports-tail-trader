from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.allocation import Allocation, AllocationPlan


def test_allocation_plan_is_data_contract_without_policy_constructor() -> None:
    plan = AllocationPlan(
        trace_id="trace",
        total_budget_usdc=Decimal("10"),
        allocations=(
            Allocation(
                condition_id="condition",
                token_id="token",
                target_budget_usdc=Decimal("10"),
                buy_budget_usdc=Decimal("5"),
            ),
        ),
    )

    assert plan.allocated_budget_usdc == Decimal("5")
    assert not hasattr(AllocationPlan, "equal_weight")
