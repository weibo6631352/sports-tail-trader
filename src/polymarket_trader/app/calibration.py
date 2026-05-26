"""定价模型校准 + Brier score / log-loss。

对每个 ``accepted=true`` 决策，按预测的 ``fair_value`` 分桶，统计市场结算后
实际命中率：

- Brier = mean((fair_value - outcome)^2)，0 = 完美
- log-loss = -mean(outcome * log(fair) + (1-outcome) * log(1-fair))

结算结果通过 ``audit_events`` 中 ``market_settled`` 事件提供：payload
``winning_token_id`` 是赢家，``decision.token_id == winning_token_id`` 视为
预测正确。
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from typing import Any, Sequence

from polymarket_trader.serialization import decimal_text
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent


def build_calibration(
    *,
    decisions: Sequence[DecisionRecord],
    settlements: Sequence[AuditEvent],
    bucket_size: Decimal = Decimal("0.05"),
) -> dict[str, Any]:
    """生成 reliability diagram 数据 + 全局 Brier / log-loss。"""

    settled_winners: dict[str, str | None] = {}
    for event in settlements:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        condition_id = event.condition_id
        if not condition_id:
            continue
        winner = payload.get("winning_token_id")
        settled_winners[condition_id] = None if winner is None else str(winner)

    buckets: dict[int, _BucketAcc] = {}
    total = 0
    with_outcome = 0
    brier_sum = 0.0
    log_loss_sum = 0.0
    log_loss_count = 0

    for record in decisions:
        fair = _extract_fair_value(record)
        if fair is None:
            continue
        total += 1
        bucket_idx = _bucket_index(fair, bucket_size)
        bucket = buckets.setdefault(bucket_idx, _BucketAcc(bucket_idx, bucket_size))
        bucket.add_prediction(fair)

        winner = settled_winners.get(record.condition_id)
        if winner is None:
            continue
        if record.token_id is None:
            continue
        with_outcome += 1
        outcome = 1.0 if record.token_id == winner else 0.0
        fair_f = float(fair)
        brier_sum += (fair_f - outcome) ** 2
        # 截断避免 log(0) → -inf；ε = 1e-6
        clipped = max(1e-6, min(1 - 1e-6, fair_f))
        log_loss_sum -= outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped)
        log_loss_count += 1
        bucket.add_outcome(outcome)

    bucket_payloads = [buckets[idx].to_payload() for idx in sorted(buckets)]
    brier = (brier_sum / log_loss_count) if log_loss_count else None
    log_loss = (log_loss_sum / log_loss_count) if log_loss_count else None
    return {
        "bucket_size": str(bucket_size),
        "buckets": bucket_payloads,
        "brier_score": None if brier is None else f"{brier:.6f}",
        "log_loss": None if log_loss is None else f"{log_loss:.6f}",
        "total_samples": total,
        "with_outcome_count": with_outcome,
    }


def _extract_fair_value(record: DecisionRecord) -> Decimal | None:
    output = record.decision_output if isinstance(record.decision_output, Mapping) else {}
    value = output.get("fair_value")
    if value is None:
        return None
    try:
        fair = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if fair < Decimal("0") or fair > Decimal("1"):
        return None
    return fair


def _bucket_index(fair: Decimal, bucket_size: Decimal) -> int:
    if bucket_size <= 0:
        return 0
    idx = int((fair / bucket_size))
    # fair=1.0 落在最后一个桶
    last_idx = int(Decimal("1") / bucket_size) - 1
    return min(idx, last_idx)


class _BucketAcc:
    __slots__ = ("idx", "size", "predictions", "outcomes")

    def __init__(self, idx: int, size: Decimal) -> None:
        self.idx = idx
        self.size = size
        self.predictions: list[Decimal] = []
        self.outcomes: list[float] = []

    def add_prediction(self, fair: Decimal) -> None:
        self.predictions.append(fair)

    def add_outcome(self, outcome: float) -> None:
        self.outcomes.append(outcome)

    def to_payload(self) -> dict[str, Any]:
        lower = self.size * Decimal(self.idx)
        upper = lower + self.size
        if upper > Decimal("1"):
            upper = Decimal("1")
        mean_pred = (
            sum(self.predictions, Decimal("0")) / Decimal(len(self.predictions))
            if self.predictions
            else None
        )
        empirical = (
            (sum(self.outcomes) / len(self.outcomes)) if self.outcomes else None
        )
        return {
            "lower": decimal_text(lower),
            "upper": decimal_text(upper),
            "prediction_count": len(self.predictions),
            "outcome_count": len(self.outcomes),
            "mean_prediction": decimal_text(mean_pred),
            "empirical_rate": None if empirical is None else f"{empirical:.6f}",
        }
