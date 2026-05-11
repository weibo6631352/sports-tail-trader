"""Outright 风控前置检查。

框架 RiskManager 仍是最终门禁；本函数是策略侧前置约束，对外暴露细粒度
拒绝原因（``OutrightRejectReason``），便于审计。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from strategies.current.outright.types import OutrightRejectReason


def check_outright_entry_risk(
    *,
    now: datetime,
    market_end_at: datetime | None,
    proposed_amount_usdc: Decimal,
    existing_outright_exposure_usdc: Decimal,
    existing_event_exposure_usdc: Decimal,
    max_per_market_usdc: Decimal,
    max_event_correlation_usdc: Decimal,
    max_total_outright_usdc: Decimal,
    max_hold_horizon_days: int,
) -> OutrightRejectReason | None:
    """通过 → 返回 None；不通过 → 返回拒绝原因。

    - ``existing_outright_exposure_usdc`` / ``existing_event_exposure_usdc``
      由调用侧聚合，本函数不负责取值（避免依赖外部 state）。
    - ``market_end_at`` 已通过 endDate 推断（Polymarket / live worker 双向修正
      过的真值），按它判断 hold horizon。
    """

    if market_end_at is not None:
        normalized_end = _to_utc(market_end_at)
        normalized_now = _to_utc(now)
        if normalized_end <= normalized_now:
            return OutrightRejectReason.MARKET_END_PASSED
        horizon_seconds = (normalized_end - normalized_now).total_seconds()
        if horizon_seconds > max_hold_horizon_days * 86400:
            return OutrightRejectReason.HOLD_HORIZON_EXCEEDED
    if existing_outright_exposure_usdc + proposed_amount_usdc > max_total_outright_usdc:
        return OutrightRejectReason.TOTAL_BUDGET_EXHAUSTED
    if existing_event_exposure_usdc + proposed_amount_usdc > max_event_correlation_usdc:
        return OutrightRejectReason.EVENT_CORRELATION_CAP
    if proposed_amount_usdc > max_per_market_usdc:
        return OutrightRejectReason.PER_MARKET_CAP_EXCEEDED
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["check_outright_entry_risk"]
