"""Outright 评估器：编排 pricing + 流动性/价格门 + 拒绝原因。

纯函数：输入 SeasonOddsSnapshot + 当前 orderbook 视图 + 配置，输出
``OutrightEvaluation``。不涉及任何 IO，全部在内存计算。

流动性 / edge / price-vs-fair 门禁走 ``_shared/edge_gates``——与 series winner
共享同一份公式，避免双口径。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot

from polymarket_trader.workflow._shared.edge_gates import (
    REASON_INSUFFICIENT_EDGE,
    REASON_LIQUIDITY_BELOW_MIN,
    REASON_MISSING_BEST_ASK,
    REASON_PRICE_ABOVE_FAIR,
    check_entry_gates,
    implied_mid_probability,
)
from polymarket_trader.workflow.outright.pricing import (
    outright_exit_price_target,
    outright_fair_value,
)
from polymarket_trader.workflow.outright.types import (
    OutrightAction,
    OutrightCandidate,
    OutrightEvaluation,
    OutrightRejectReason,
    outright_action_for_permission,
    to_tail_action,
)
from polymarket_trader.workflow.tail.types import ExecutionPermission


_GATE_REASON_TO_OUTRIGHT: dict[str, OutrightRejectReason] = {
    REASON_MISSING_BEST_ASK: OutrightRejectReason.MISSING_BEST_ASK,
    REASON_LIQUIDITY_BELOW_MIN: OutrightRejectReason.LIQUIDITY_BELOW_MIN,
    REASON_INSUFFICIENT_EDGE: OutrightRejectReason.INSUFFICIENT_EDGE,
    REASON_PRICE_ABOVE_FAIR: OutrightRejectReason.PRICE_ABOVE_FAIR,
}


def evaluate_outright_opportunity(
    *,
    snapshot: SeasonOddsSnapshot | None,
    market: Market,
    outcome_label: str,
    token_id: str,
    best_ask: Decimal | None,
    best_bid: Decimal | None = None,
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
        market_slug=market.market_slug,
        condition_id=market.condition_id,
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
    pricing_result = outright_fair_value(snapshot, market, outcome_label)
    fair_value = pricing_result.value
    if fair_value is None:
        # 拒绝原因来自 pricing：区分 OUTRIGHT_TEAM_NOT_RESOLVED /
        # SEASON_ODDS_INCOMPLETE / ODDS_OUTCOME_NOT_MAPPED，供 §10 审计。
        return _reject(
            candidate,
            pricing_result.reject or OutrightRejectReason.ODDS_OUTCOME_NOT_MAPPED,
        )
    gate = check_entry_gates(
        fair_value=fair_value,
        best_ask=best_ask,
        buyable_liquidity_usdc=buyable_liquidity_usdc,
        min_edge_bps=min_edge_bps,
        max_entry_price=max_entry_price,
        min_orderbook_depth_usdc=min_orderbook_depth_usdc,
    )
    if not gate.passed:
        reason = _GATE_REASON_TO_OUTRIGHT[gate.reason or REASON_MISSING_BEST_ASK]
        return _reject(
            candidate,
            reason,
            fair_value=fair_value,
            entry_price_cap=gate.entry_price_cap,
            metadata=dict(gate.metadata),
        )
    # gate.passed 时 entry_price_cap 与 best_ask 一定非空：见 check_entry_gates 第一道门。
    entry_cap = gate.entry_price_cap
    if entry_cap is None or best_ask is None:
        raise AssertionError("edge_gates.check_entry_gates returned passed=True without cap/best_ask")
    exit_target = outright_exit_price_target(
        fair_value,
        best_ask,
        exit_edge_target=exit_edge_target,
        min_profit_per_share=min_profit_per_share,
    )
    action = outright_action_for_permission(execution_permission)
    implied = implied_mid_probability(best_bid, best_ask)
    metadata: dict[str, Any] = {
        "fair_value": str(fair_value),
        "entry_price_cap": str(entry_cap),
        "exit_price_target": str(exit_target),
        "best_ask": str(best_ask),
        "snapshot_age_seconds": age,
        "tail_action": to_tail_action(action).value,
    }
    if implied is not None:
        metadata["implied_mid_prob"] = str(implied)
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
