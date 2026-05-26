"""Series 风控前置检查。

框架 RiskManager 仍是最终门禁；本函数是策略侧前置约束，对外暴露细粒度
拒绝原因（``SeriesRejectReason``），便于审计。

设计：sub_type 之间共享同一组校验逻辑（horizon / 预算 / 相关性 / per-market），
配置（budget / cap / horizon）由调用方按 sub_type 注入——避免在 risk 函数里
重复出现 WINNER / TOTAL_GAMES / HANDICAP 三条分支。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.workflow.series.types import SeriesRejectReason


@dataclass(frozen=True, slots=True)
class SeriesSubTypeRiskConfig:
    """单个 sub_type（WINNER / TOTAL_GAMES / HANDICAP）的风控参数。"""

    max_per_market_usdc: Decimal
    max_event_correlation_usdc: Decimal
    max_total_series_usdc: Decimal
    max_hold_horizon_days: int
    min_remaining_days: int


def check_series_entry_risk(
    *,
    now: datetime,
    market_end_at: datetime | None,
    proposed_amount_usdc: Decimal,
    existing_series_exposure_usdc: Decimal,
    existing_event_exposure_usdc: Decimal,
    config: SeriesSubTypeRiskConfig,
) -> SeriesRejectReason | None:
    """通过 → 返回 None；不通过 → 返回拒绝原因。"""

    if market_end_at is not None:
        normalized_end = _to_utc(market_end_at)
        normalized_now = _to_utc(now)
        if normalized_end <= normalized_now:
            return SeriesRejectReason.MARKET_END_PASSED
        remaining_seconds = (normalized_end - normalized_now).total_seconds()
        if remaining_seconds > config.max_hold_horizon_days * 86400:
            return SeriesRejectReason.HOLD_HORIZON_EXCEEDED
        if config.min_remaining_days > 0 and remaining_seconds < config.min_remaining_days * 86400:
            return SeriesRejectReason.MIN_REMAINING_DAYS_NOT_MET
    # 预算检查顺序：total → event-corr → per-market。先 total 是因为它代表
    # 全策略最高约束，违反时改其他参数也无法救。
    if existing_series_exposure_usdc + proposed_amount_usdc > config.max_total_series_usdc:
        return SeriesRejectReason.TOTAL_BUDGET_EXHAUSTED
    if existing_event_exposure_usdc + proposed_amount_usdc > config.max_event_correlation_usdc:
        return SeriesRejectReason.EVENT_CORRELATION_CAP
    if proposed_amount_usdc > config.max_per_market_usdc:
        return SeriesRejectReason.PER_MARKET_CAP_EXCEEDED
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["SeriesSubTypeRiskConfig", "check_series_entry_risk"]
