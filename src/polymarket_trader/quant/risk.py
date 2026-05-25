"""体育扫尾策略级风险限制。

只保留连续亏损暂停这个安全网。预算分配**无条件信任 Kelly Criterion**:
Kelly 自带单笔 fraction 上限和 drawdown halt,在 Kelly 之上叠加 per-event /
per-league / per-day exposure cap 都是次优——会拒掉有 edge 的 condition,
违背 "不放过任何盈利市场" + "多盘口分散风险" 的核心哲学。

框架级账户余额、单笔金额、订单状态和执行器门禁仍由 ``RiskManager`` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from polymarket_trader.quant.config import CurrentStrategyConfig


@dataclass(frozen=True, slots=True)
class SportsRiskDecision:
    """体育扫尾策略级风险判断结果。"""

    passed: bool
    reason: str = "passed"
    metadata: Mapping[str, object] | None = None


def check_tail_entry_risk(
    config: CurrentStrategyConfig,
    *,
    metadata: Mapping[str, object],
) -> SportsRiskDecision:
    """检查策略级安全网。当前仅:连续亏损 N 次暂停。

    Kelly 已经是最优单笔 sizing,不在它之上再叠加任何 exposure cap。
    portfolio 级 drawdown halt 由 RiskManager / Kelly 算法自身覆盖。
    """

    risk_metadata: dict[str, object] = {"risk_reason": "pending"}
    consecutive_losses = _int_metadata(
        metadata,
        "tail_consecutive_losses",
        "consecutive_losses",
        default=0,
    )
    risk_metadata["consecutive_losses"] = consecutive_losses
    if (
        config.tail_max_consecutive_losses >= 0
        and consecutive_losses >= config.tail_max_consecutive_losses
    ):
        risk_metadata["risk_reason"] = "consecutive_loss_pause"
        return SportsRiskDecision(
            passed=False,
            reason="consecutive_loss_pause",
            metadata=risk_metadata,
        )

    risk_metadata["risk_reason"] = "passed"
    return SportsRiskDecision(passed=True, metadata=risk_metadata)


def _int_metadata(
    metadata: Mapping[str, object],
    *keys: str,
    default: int,
) -> int:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (ValueError, TypeError):
            continue
    return default
