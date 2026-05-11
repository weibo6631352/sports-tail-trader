"""Outright 评估器：编排 pricing + 流动性/价格门 + 拒绝原因。

纯函数：输入 SeasonOddsSnapshot + 当前 orderbook 视图 + 配置，输出
``OutrightEvaluation``。不涉及任何 IO，全部在内存计算。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.sports_season import SeasonOddsSnapshot

from strategies.current.outright.pricing import (
    outright_entry_price_cap,
    outright_exit_price_target,
    outright_fair_value,
)
from strategies.current.outright.types import (
    OutrightAction,
    OutrightCandidate,
    OutrightEvaluation,
    OutrightRejectReason,
    outright_action_for_permission,
    to_tail_action,
)
from strategies.current.tail.types import ExecutionPermission


def evaluate_outright_opportunity(
    *,
    snapshot: SeasonOddsSnapshot | None,
    market_slug: str,
    condition_id: str,
    outcome_label: str,
    token_id: str,
    best_ask: Decimal | None,
    buyable_liquidity_usdc: Decimal,
    now: datetime,
    max_season_odds_age_seconds: int,
    min_edge_bps: int,
    max_entry_price: Decimal,
    min_orderbook_depth_usdc: Decimal,
    exit_edge_target: Decimal,
    min_profit_per_share: Decimal,
    execution_permission: ExecutionPermission,
    extra_metadata: Mapping[str, Any] | None = None,
) -> OutrightEvaluation:
    """评估一个 outright outcome 是否值得入场。"""

    candidate = OutrightCandidate(
        market_slug=market_slug,
        condition_id=condition_id,
        outcome_label=outcome_label,
        token_id=token_id,
        metadata=dict(extra_metadata or {}),
    )
    if snapshot is None:
        return _reject(candidate, OutrightRejectReason.MISSING_SEASON_ODDS)
    age = _age_seconds(snapshot.observed_at, now)
    if age > max_season_odds_age_seconds:
        return _reject(
            candidate,
            OutrightRejectReason.STALE_SEASON_ODDS,
            metadata={"snapshot_age_seconds": age},
        )
    fair_value = outright_fair_value(snapshot, outcome_label)
    if fair_value is None:
        return _reject(candidate, OutrightRejectReason.ODDS_OUTCOME_NOT_MAPPED)
    if best_ask is None:
        return _reject(candidate, OutrightRejectReason.MISSING_BEST_ASK, fair_value=fair_value)
    if buyable_liquidity_usdc < min_orderbook_depth_usdc:
        return _reject(
            candidate,
            OutrightRejectReason.LIQUIDITY_BELOW_MIN,
            fair_value=fair_value,
            metadata={"buyable_liquidity_usdc": str(buyable_liquidity_usdc)},
        )
    entry_cap = outright_entry_price_cap(
        fair_value,
        min_edge_bps=min_edge_bps,
        max_entry_price=max_entry_price,
    )
    if best_ask > entry_cap:
        return _reject(
            candidate,
            OutrightRejectReason.INSUFFICIENT_EDGE,
            fair_value=fair_value,
            entry_price_cap=entry_cap,
            metadata={"best_ask": str(best_ask)},
        )
    if best_ask >= fair_value:
        return _reject(
            candidate,
            OutrightRejectReason.PRICE_ABOVE_FAIR,
            fair_value=fair_value,
            entry_price_cap=entry_cap,
            metadata={"best_ask": str(best_ask)},
        )
    exit_target = outright_exit_price_target(
        fair_value,
        best_ask,
        exit_edge_target=exit_edge_target,
        min_profit_per_share=min_profit_per_share,
    )
    action = outright_action_for_permission(execution_permission)
    metadata: dict[str, Any] = {
        "fair_value": str(fair_value),
        "entry_price_cap": str(entry_cap),
        "exit_price_target": str(exit_target),
        "best_ask": str(best_ask),
        "snapshot_age_seconds": age,
        "tail_action": to_tail_action(action).value,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return OutrightEvaluation(
        accepted=True,
        action=action,
        reason="outright_entry_accepted",
        candidate=candidate,
        execution_permission=execution_permission,
        fair_value=fair_value,
        entry_price_cap=entry_cap,
        exit_price_target=exit_target,
        metadata=metadata,
    )


def _reject(
    candidate: OutrightCandidate,
    reason: OutrightRejectReason,
    *,
    fair_value: Decimal | None = None,
    entry_price_cap: Decimal | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> OutrightEvaluation:
    payload: dict[str, Any] = {"reject_reason": reason.value}
    if fair_value is not None:
        payload["fair_value"] = str(fair_value)
    if entry_price_cap is not None:
        payload["entry_price_cap"] = str(entry_price_cap)
    if metadata:
        payload.update(metadata)
    return OutrightEvaluation(
        accepted=False,
        action=OutrightAction.REJECT,
        reason=reason.value,
        candidate=candidate,
        reject_reason=reason,
        fair_value=fair_value,
        entry_price_cap=entry_price_cap,
        metadata=payload,
    )


def _age_seconds(observed_at: datetime, now: datetime) -> int:
    a = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    b = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return max(0, int((b - a).total_seconds()))


__all__ = ["evaluate_outright_opportunity"]
