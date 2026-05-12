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
    # 33 × 33 × 33 = 35937 > 1000；所有候选值都在金融允许范围内
    candidates = {
        "tail_outright_min_edge_bps": list(range(33)),
        "tail_outright_max_entry_price": [round(0.01 * i, 4) for i in range(1, 34)],
        "tail_outright_min_orderbook_depth_usdc": [float(i) for i in range(33)],
    }
    with pytest.raises(ValueError, match="too large"):
        build_parameter_sweep(decisions=(), settlements=(), candidates=candidates)


# === F-2 范围 guard ===

def test_range_guard_rejects_negative_min_edge_bps() -> None:
    with pytest.raises(ValueError, match="tail_outright_min_edge_bps.*minimum"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"tail_outright_min_edge_bps": [-1]},
        )


def test_range_guard_rejects_max_entry_price_at_one() -> None:
    with pytest.raises(ValueError, match="tail_outright_max_entry_price.*< 1"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"tail_outright_max_entry_price": ["1.0"]},
        )


def test_range_guard_rejects_max_entry_price_at_zero() -> None:
    with pytest.raises(ValueError, match="tail_outright_max_entry_price.*> 0"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"tail_outright_max_entry_price": ["0"]},
        )


def test_range_guard_rejects_negative_max_entry_price() -> None:
    with pytest.raises(ValueError, match="tail_outright_max_entry_price.*> 0"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"tail_outright_max_entry_price": ["-0.5"]},
        )


def test_range_guard_rejects_negative_min_orderbook_depth() -> None:
    with pytest.raises(ValueError, match="tail_outright_min_orderbook_depth_usdc.*minimum"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"tail_outright_min_orderbook_depth_usdc": ["-1"]},
        )


def test_range_guard_rejects_entry_no_price_max_above_one() -> None:
    with pytest.raises(ValueError, match="entry_no_price_max.*maximum"):
        build_parameter_sweep(
            decisions=(),
            settlements=(),
            candidates={"entry_no_price_max": ["1.0001"]},
        )


def test_range_guard_allows_entry_no_price_max_at_one() -> None:
    # 端点 1 表示"不设上限"，是合法值
    result = build_parameter_sweep(
        decisions=(),
        settlements=(),
        candidates={"entry_no_price_max": ["1.0"]},
    )
    assert result["candidate_count"] == 1


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


def test_entry_price_cap_fallback_counted_and_surfaced() -> None:
    """没 entry_price / metadata.best_ask 时退回 entry_price_cap——偏差应暴露
    在 ``entry_price_cap_fallback_count`` 字段，让 caller 知道结果有偏。"""

    decisions = (
        DecisionRecord(
            strategy_id="sports_tail",
            record_id="r1",
            trace_id="trace-r1",
            condition_id="c1",
            token_id="t1",
            decision_input={},
            decision_output={
                "fair_value": "0.50",
                # 故意不给 entry_price 也不给 metadata.best_ask；只给 cap
                "entry_price_cap": "0.40",
            },
            accepted=False,
            reason="record_only",
            created_at=BASE,
        ),
    )
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=(),
        candidates={"tail_outright_min_edge_bps": [100]},
    )
    assert result["entry_price_cap_fallback_count"] == 1
    # 仍可评分（不是 unscorable）
    assert result["scorable_decision_count"] == 1


def test_entry_price_cap_fallback_zero_when_metadata_has_best_ask() -> None:
    decisions = (
        DecisionRecord(
            strategy_id="sports_tail",
            record_id="r1",
            trace_id="trace-r1",
            condition_id="c1",
            token_id="t1",
            decision_input={},
            decision_output={
                "fair_value": "0.50",
                "metadata": {"best_ask": "0.42"},
                "entry_price_cap": "0.40",
            },
            accepted=False,
            reason="record_only",
            created_at=BASE,
        ),
    )
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=(),
        candidates={"tail_outright_min_edge_bps": [100]},
    )
    assert result["entry_price_cap_fallback_count"] == 0
    assert result["scorable_decision_count"] == 1


def test_outright_nested_metadata_resolves_real_best_ask() -> None:
    """outright evaluator 写的 shape：fair_value / best_ask 都嵌在
    ``metadata.outright_metadata`` 里，price 字段是 entry_price_cap。
    reader 必须取 outright_metadata.best_ask 当 entry_price，避免假 fallback。"""

    decisions = (
        DecisionRecord(
            strategy_id="sports_tail",
            record_id="r1",
            trace_id="trace-r1",
            condition_id="c1",
            token_id="t1",
            decision_input={},
            decision_output={
                "action": "buy",
                "price": "0.40",  # = entry_price_cap，故意写顶层
                "metadata": {
                    "market_family": "outright",
                    "outright_metadata": {
                        "fair_value": "0.50",
                        "entry_price_cap": "0.40",
                        "best_ask": "0.45",
                        "buyable_liquidity_usdc": "200",
                    },
                    "outright_fair_value": "0.50",
                },
            },
            accepted=True,
            reason="outright_entry_accepted",
            created_at=BASE,
        ),
    )
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=(),
        candidates={"tail_outright_min_edge_bps": [100]},
    )
    assert result["entry_price_cap_fallback_count"] == 0
    assert result["scorable_decision_count"] == 1
    # predicted_edge_bps 应基于真实 best_ask=0.45（edge=(0.50-0.45)/0.50=1000bps）
    # 而不是 cap=0.40（edge=2000bps）；100bps 门槛下都过，但用更严格门槛验证
    strict = build_parameter_sweep(
        decisions=decisions,
        settlements=(),
        candidates={"tail_outright_min_edge_bps": [1500]},
    )
    assert strict["results"][0]["would_have_entered_count"] == 0


def test_outright_skip_only_outright_metadata_resolves_real_best_ask() -> None:
    """outright SKIP/record-only：顶层无 price/fair_value/entry_price_cap，
    全部信息都在 metadata.outright_metadata 里。reader 不应判 unscorable。"""

    decisions = (
        DecisionRecord(
            strategy_id="sports_tail",
            record_id="r1",
            trace_id="trace-r1",
            condition_id="c1",
            token_id="t1",
            decision_input={},
            decision_output={
                "action": "skip",
                "reason": "outright_record_only",
                "metadata": {
                    "market_family": "outright",
                    "outright_metadata": {
                        "fair_value": "0.60",
                        "entry_price_cap": "0.48",
                        "best_ask": "0.50",
                    },
                },
            },
            accepted=False,
            reason="outright_record_only",
            created_at=BASE,
        ),
    )
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=(),
        candidates={"tail_outright_min_edge_bps": [100]},
    )
    assert result["scorable_decision_count"] == 1
    assert result["unscorable_decision_count"] == 0
    assert result["entry_price_cap_fallback_count"] == 0


def test_tail_flat_price_field_used_as_best_ask() -> None:
    """tail (非 outright) entry 决策：decision_output["price"] 就是 best_ask
    （hooks.decide_entry 把盘口 best_ask 直接写到 ExtensionDecision.price）。
    缺顶层 entry_price 时不应判 unscorable 也不应走 cap fallback。"""

    decisions = (
        DecisionRecord(
            strategy_id="sports_tail",
            record_id="r1",
            trace_id="trace-r1",
            condition_id="c1",
            token_id="t1",
            decision_input={},
            decision_output={
                "action": "buy",
                "price": "0.42",
                "fair_value": "0.50",
                # 无 outright_metadata、market_family != outright
                "metadata": {},
            },
            accepted=True,
            reason="strategy_entry",
            created_at=BASE,
        ),
    )
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=(),
        candidates={"tail_outright_min_edge_bps": [100]},
    )
    assert result["entry_price_cap_fallback_count"] == 0
    assert result["scorable_decision_count"] == 1


def test_outright_does_not_misuse_price_field_as_best_ask() -> None:
    """关键反例：outright BUY 的 ``price`` 字段是 entry_price_cap，不是 best_ask。
    若 outright 嵌套元数据缺 best_ask，reader 不应退到顶层 price，避免把 cap
    当作 best_ask 算 PnL 导致偏高。应走显式 fallback。"""

    decisions = (
        DecisionRecord(
            strategy_id="sports_tail",
            record_id="r1",
            trace_id="trace-r1",
            condition_id="c1",
            token_id="t1",
            decision_input={},
            decision_output={
                "action": "buy",
                "price": "0.40",  # = entry_price_cap
                "metadata": {
                    "market_family": "outright",
                    "outright_metadata": {
                        "fair_value": "0.50",
                        "entry_price_cap": "0.40",
                        # 故意不写 best_ask
                    },
                },
            },
            accepted=True,
            reason="outright_entry_accepted",
            created_at=BASE,
        ),
    )
    result = build_parameter_sweep(
        decisions=decisions,
        settlements=(),
        candidates={"tail_outright_min_edge_bps": [100]},
    )
    # 必须 fallback 而不是把 price=0.40 当 best_ask
    assert result["entry_price_cap_fallback_count"] == 1
    assert result["scorable_decision_count"] == 1


def test_sweep_sample_quality_enum_values_are_stable() -> None:
    """fallback count 串到前端 + 历史 record 同义比较，enum 值不能漂。"""

    from polymarket_trader.app.parameter_sweep import SweepSampleQuality

    assert SweepSampleQuality.REAL_BEST_ASK.value == "real_best_ask"
    assert SweepSampleQuality.ENTRY_PRICE_CAP_FALLBACK.value == "entry_price_cap_fallback"
    # StrEnum 行为：member 与字符串相等，便于历史日志/字符串比较
    assert SweepSampleQuality.REAL_BEST_ASK == "real_best_ask"


def test_coerce_value_raises_for_unknown_type_name() -> None:
    """Exhaustiveness guard: _coerce_value unknown spec.type_name raises ValueError."""

    from polymarket_trader.app.parameter_sweep import _ParameterSpec, _coerce_value

    spec = _ParameterSpec("string")
    with pytest.raises(ValueError, match="unknown spec: string"):
        _coerce_value("some_key", "foo", spec)
