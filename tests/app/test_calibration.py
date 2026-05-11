"""``build_calibration`` 数学正确性。

覆盖：
- Brier score 计算（mean((fair - outcome)^2)）
- log-loss 计算
- bucket 划分（0.05 步长）
- 缺失结算的决策不计入 brier_sum
- 未结算决策被 bucket 但 outcome_count = 0
- ``winning_token_id`` 与 ``record.token_id`` 比较
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from polymarket_trader.app.calibration import build_calibration
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent


BASE = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


def _decision(*, record_id: str, condition_id: str, token_id: str, fair: str) -> DecisionRecord:
    return DecisionRecord(
        strategy_id="sports_tail",
        record_id=record_id,
        trace_id=f"trace-{record_id}",
        condition_id=condition_id,
        token_id=token_id,
        decision_input={},
        decision_output={"fair_value": fair},
        accepted=True,
        reason=None,
        created_at=BASE,
    )


def _settlement(*, condition_id: str, winner_token: str | None) -> AuditEvent:
    return AuditEvent(
        strategy_id="sports_tail",
        trace_id=f"trace-{condition_id}",
        event_id=f"settled-{condition_id}",
        event_title="market_settled",
        condition_id=condition_id,
        token_id=winner_token,
        status="ok",
        reason="manual_settlement",
        payload={"winning_token_id": winner_token},
        created_at=BASE,
    )


def test_brier_perfect_predictor() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair="1.00"),
        _decision(record_id="r2", condition_id="c2", token_id="t2", fair="0.00"),
    )
    settlements = (
        _settlement(condition_id="c1", winner_token="t1"),  # winner = predicted token, outcome=1
        _settlement(condition_id="c2", winner_token="t-other"),  # outcome=0
    )
    payload = build_calibration(decisions=decisions, settlements=settlements)
    # fair=1, outcome=1 → (1-1)^2 = 0
    # fair=0, outcome=0 → (0-0)^2 = 0
    # 但 log-loss 端点要 clip 避免 log(0)；用 1e-6 截断后还是非常小
    assert float(payload["brier_score"]) == 0.0
    assert payload["with_outcome_count"] == 2


def test_brier_random_predictor_around_quarter() -> None:
    # fair=0.5, outcome 任意 → (0.5-1)^2=0.25, (0.5-0)^2=0.25
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair="0.50"),
        _decision(record_id="r2", condition_id="c2", token_id="t2", fair="0.50"),
    )
    settlements = (
        _settlement(condition_id="c1", winner_token="t1"),
        _settlement(condition_id="c2", winner_token="t-other"),
    )
    payload = build_calibration(decisions=decisions, settlements=settlements)
    assert float(payload["brier_score"]) == 0.25


def test_decision_without_settlement_excluded_from_brier() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair="0.50"),
        _decision(record_id="r2", condition_id="c2", token_id="t2", fair="0.30"),  # 无结算
    )
    settlements = (
        _settlement(condition_id="c1", winner_token="t1"),
    )
    payload = build_calibration(decisions=decisions, settlements=settlements)
    assert payload["total_samples"] == 2
    assert payload["with_outcome_count"] == 1
    # 仅 c1 → Brier = (0.5-1)^2 = 0.25
    assert float(payload["brier_score"]) == 0.25


def test_buckets_partitioned_by_fair_value() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair="0.05"),  # 桶 0
        _decision(record_id="r2", condition_id="c2", token_id="t2", fair="0.45"),  # 桶 9 (0.45/0.05=9)
        _decision(record_id="r3", condition_id="c3", token_id="t3", fair="0.95"),  # 桶 19
    )
    payload = build_calibration(decisions=decisions, settlements=(), bucket_size=Decimal("0.05"))
    # 应该有 3 个非空 bucket
    non_empty = [b for b in payload["buckets"] if b["prediction_count"] > 0]
    assert len(non_empty) == 3


def test_log_loss_clipped_for_extreme_predictions() -> None:
    # fair=1.0 但 outcome=0 → 不 clip 会是 -inf
    decisions = (_decision(record_id="r1", condition_id="c1", token_id="t1", fair="1.00"),)
    settlements = (_settlement(condition_id="c1", winner_token="t-other"),)
    payload = build_calibration(decisions=decisions, settlements=settlements)
    log_loss = float(payload["log_loss"])
    assert math.isfinite(log_loss)
    assert log_loss > 0


def test_invalid_fair_value_skipped() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", fair="1.5"),  # out of [0,1]
        _decision(record_id="r2", condition_id="c2", token_id="t2", fair="bogus"),  # not a number
        _decision(record_id="r3", condition_id="c3", token_id="t3", fair="0.50"),  # valid
    )
    payload = build_calibration(decisions=decisions, settlements=())
    assert payload["total_samples"] == 1
