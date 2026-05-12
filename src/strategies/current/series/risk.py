"""Series WINNER 风控前置检查。

框架 RiskManager 仍是最终门禁；本函数是策略侧前置约束，对外暴露细粒度
拒绝原因（``SeriesRejectReason``），便于审计。

与 ``outright/risk`` 结构对齐，但语义独立：series WINNER 持有期通常远短于
outright（一个系列赛 1-3 周），horizon 默认 30 天即可。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from strategies.current.series.types import SeriesRejectReason


def check_series_entry_risk(
    *,
    now: datetime,
    market_end_at: datetime | None,
    proposed_amount_usdc: Decimal,
    existing_series_exposure_usdc: Decimal,
    existing_event_exposure_usdc: Decimal,
    max_per_market_usdc: Decimal,
    max_event_correlation_usdc: Decimal,
    max_total_series_usdc: Decimal,
    max_hold_horizon_days: int,
    min_remaining_days: int,
) -> SeriesRejectReason | None:
    """通过 → 返回 None；不通过 → 返回拒绝原因。"""

    if market_end_at is not None:
        normalized_end = _to_utc(market_end_at)
        normalized_now = _to_utc(now)
        if normalized_end <= normalized_now:
            return SeriesRejectReason.MARKET_END_PASSED
        remaining_seconds = (normalized_end - normalized_now).total_seconds()
        if remaining_seconds > max_hold_horizon_days * 86400:
            return SeriesRejectReason.HOLD_HORIZON_EXCEEDED
        if min_remaining_days > 0 and remaining_seconds < min_remaining_days * 86400:
            return SeriesRejectReason.MIN_REMAINING_DAYS_NOT_MET
    # 预算检查顺序：total → event-corr → per-market。先 total 是因为它代表
    # 全策略最高约束，违反时改其他参数也无法救。
    if existing_series_exposure_usdc + proposed_amount_usdc > max_total_series_usdc:
        return SeriesRejectReason.TOTAL_BUDGET_EXHAUSTED
    if existing_event_exposure_usdc + proposed_amount_usdc > max_event_correlation_usdc:
        return SeriesRejectReason.EVENT_CORRELATION_CAP
    if proposed_amount_usdc > max_per_market_usdc:
        return SeriesRejectReason.PER_MARKET_CAP_EXCEEDED
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["check_series_entry_risk"]
