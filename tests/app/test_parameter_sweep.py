"""``build_parameter_sweep`` 数学正确性 + 守门逻辑。

覆盖：
- 候选键白名单校验（不在白名单返回 ValueError）
- 候选值空列表返回 ValueError
- 笛卡尔积上限 1000 守门
- ``int`` / ``decimal`` coerce 失败抛 ValueError
- 单维度 sweep 行为：min_edge_bps 提高时通过样本减少
- 多维度笛卡尔积：长度 = 各维度乘积
- 缺 fair_value / entry_price 的决策计入 unscorable
- 缺 liquidity 数据 + ``min_depth_usdc`` 设置时保守不通过
- ``win_rate`` 与 ``hypothetical_pnl_usdc`` 数学正确
- ``best_by_pnl`` / ``best_by_win_rate`` 选择正确
- pending（无 settlement）不影响 PnL 但计入 ``pending_unsettled_count``
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.app.parameter_sweep import (
    MAX_GRID_COMBINATIONS,
    build_parameter_sweep,
    supported_parameter_keys,
)
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent


BASE = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


def _decision(
    *,
    record_id: str,
    condition_id: str,
    token_id: str | None,
    fair_value: str | None = "0.50",
    entry_price: str | None = "0.40",
    liquidity_usdc: str | None = None,
    accepted: bool = False,
) -> DecisionRecord:
    output: dict[str, Any] = {}
    if fair_value is not None:
        output["fair_value"] = fair_value
    if entry_price is not None:
        output["entry_price"] = entry_price
    if liquidity_usdc is not None:
        output["metadata"] = {"buyable_liquidity_usdc": liquidity_usdc}
    return DecisionRecord(
        strategy_id="sports_tail",
        record_id=record_id,
        trace_id=f"trace-{record_id}",
        condition_id=condition_id,
        token_id=token_id,
        decision_input={},
        decision_output=output,
        accepted=accepted,
        reason=None if accepted else "skip",
        created_at=BASE,
    )


def _settlement(*, condition_id: str, winner: str | None) -> AuditEvent:
    return AuditEvent(
        strategy_id="sports_tail",
        trace_id=f"trace-{condition_id}",
        event_id=f"settled-{condition_id}",
        event_title="market_settled",
        condition_id=condition_id,
        token_id=winner,
        status="ok",
        reason="manual",
        payload={"winning_token_id": winner},
        created_at=BASE,
    )


def test_supported_keys_are_stable_sorted() -> None:
    keys = supported_parameter_keys()
    assert keys == tuple(sorted(keys))
    assert "tail_outright_min_edge_bps" in keys


def test_unknown_candidate_key_raises() -> None:
    with pytest.raises(ValueError, match="unsupported sweep parameter"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"bogus_param": [1]},
        )


def test_empty_candidates_raises() -> None:
    with pytest.raises(ValueError, match="must contain at least one"):
        build_parameter_sweep(decisions=(), settlements=(), candidates={})


def test_empty_value_list_raises() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"tail_outright_min_edge_bps": []},
        )


def test_grid_size_cap_enforced() -> None:
    # 33 × 33 × 33 = 35937 > 1000
    candidates = {
        "tail_outright_min_edge_bps": list(range(33)),
        "tail_outright_max_entry_price": [0.1 * i for i in range(1, 34)],
        "tail_outright_min_orderbook_depth_usdc": [float(i) for i in range(33)],
    }
    with pytest.raises(ValueError, match="too large"):
        build_parameter_sweep(decisions=(), settlements=(), candidates=candidates)


def test_int_coerce_failure_raises() -> None:
    with pytest.raises(ValueError, match="expected int"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"tail_outright_min_edge_bps": ["not-an-int"]},
        )


def test_decimal_coerce_failure_raises() -> None:
    with pytest.raises(ValueError, match="expected decimal"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"tail_outright_max_entry_price": ["not-a-decimal"]},
        )


def test_single_dimension_higher_min_edge_filters_more() -> None:
    # fair=0.50, entry=0.40 → edge = (0.5-0.4)/0.5 * 10000 = 2000 bps
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1"),
    )
    settlements = (_settlement(condition_id="c1", winner="t1"),)
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=settlements,
        candidates={"tail_outright_min_edge_bps": [1000, 2500]},
    )
    by_param = {
        r["parameters"]["tail_outright_min_edge_bps"]: r for r in result["results"]
    }
    # 1000 bps 通过；2500 bps 不通过
    assert by_param[1000]["would_have_entered_count"] == 1
    assert by_param[2500]["would_have_entered_count"] == 0


def test_grid_cartesian_product_length() -> None:
    candidates = {
        "tail_outright_min_edge_bps": [100, 500, 1000],
        "tail_outright_max_entry_price": [0.5, 0.9],
    }
    result = build_parameter_sweep(
        decisions=(),
        settlements=(),
        candidates=candidates,
    )
    assert result["candidate_count"] == 6
    assert len(result["results"]) == 6


def test_missing_fair_value_marked_unscorable() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair_value=None),
        _decision(record_id="r2", condition_id="c2", token_id="t2"),
    )
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=(),
        candidates={"tail_outright_min_edge_bps": [100]},
    )
    assert result["unscorable_decision_count"] == 1
    assert result["scorable_decision_count"] == 1


def test_missing_liquidity_with_depth_threshold_excluded() -> None:
    # 不给 liquidity，深度门槛设置时应保守不通过
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", liquidity_usdc=None),
    )
    settlements = (_settlement(condition_id="c1", winner="t1"),)
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=settlements,
        candidates={"tail_outright_min_orderbook_depth_usdc": [10]},
    )
    assert result["results"][0]["would_have_entered_count"] == 0


def test_pnl_math_winner() -> None:
    # fair=0.50 entry=0.40 → 通过 (1000bps)
    # size = 10 / 0.4 = 25; PnL = (1 - 0.4) * 25 = 15
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1"),
    )
    settlements = (_settlement(condition_id="c1", winner="t1"),)
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=settlements,
        candidates={"tail_outright_min_edge_bps": [100]},
        per_decision_usdc=Decimal("10"),
    )
    row = result["results"][0]
    assert Decimal(row["hypothetical_pnl_usdc"]) == Decimal("15")
    assert row["win_count"] == 1
    assert row["loss_count"] == 0


def test_pnl_math_loser() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t-loser"),
    )
    settlements = (_settlement(condition_id="c1", winner="t-winner"),)
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=settlements,
        candidates={"tail_outright_min_edge_bps": [100]},
        per_decision_usdc=Decimal("10"),
    )
    row = result["results"][0]
    # size = 25; PnL = (0 - 0.4) * 25 = -10
    assert Decimal(row["hypothetical_pnl_usdc"]) == Decimal("-10")
    assert row["loss_count"] == 1


def test_pending_settlement_counted_separately() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c-pending", token_id="t1"),
    )
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=(),
        candidates={"tail_outright_min_edge_bps": [100]},
    )
    row = result["results"][0]
    assert row["would_have_entered_count"] == 1
    assert row["pending_unsettled_count"] == 1
    assert row["settled_count"] == 0
    assert Decimal(row["hypothetical_pnl_usdc"]) == Decimal("0")


def test_best_by_pnl_picks_highest() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1"),
    )
    settlements = (_settlement(condition_id="c1", winner="t1"),)
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=settlements,
        candidates={"tail_outright_min_edge_bps": [100, 2500]},
    )
    # 2500 不通过 PnL=0；100 通过 PnL=15
    assert result["best_by_pnl"]["parameters"]["tail_outright_min_edge_bps"] == 100
    assert Decimal(result["best_by_pnl"]["hypothetical_pnl_usdc"]) == Decimal("15")


def test_best_by_win_rate_picks_highest_with_settled() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1"),
        _decision(record_id="r2", condition_id="c2", token_id="t2", entry_price="0.20"),
    )
    # c1 winner = t1（赢），c2 winner = t-other（输）
    settlements = (
        _settlement(condition_id="c1", winner="t1"),
        _settlement(condition_id="c2", winner="t-other"),
    )
    # 候选 [100, 2000]
    # min_edge=100: 两条都通过 → 1 win + 1 loss → win_rate 0.5
    # min_edge=2000: 只有 c2 通过 (edge 6000bps) → 1 loss → win_rate 0
    # min_edge=4000: c2 通过 (edge 6000bps, fair=0.5 entry=0.2)，输 → win_rate 0
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=settlements,
        candidates={"tail_outright_min_edge_bps": [100, 2500]},
    )
    by_min = {
        r["parameters"]["tail_outright_min_edge_bps"]: r for r in result["results"]
    }
    assert by_min[100]["win_count"] == 1
    assert by_min[100]["loss_count"] == 1
    assert by_min[2500]["loss_count"] == 1
    # best by win rate 应该是 100
    assert result["best_by_win_rate"]["parameters"]["tail_outright_min_edge_bps"] == 100


def test_grid_cap_constant_is_reasonable() -> None:
    # 防回归——别人改 cap 时应该清楚改了什么
    assert MAX_GRID_COMBINATIONS == 1_000
