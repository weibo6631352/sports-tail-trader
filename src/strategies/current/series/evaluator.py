"""Series 评估器。

按 sub_type 分派：
- WINNER → series_win_probability(state, p_per_game) 作为 fair_value，
  edge_gates 决定 entry/reject。
- TOTAL_GAMES / GAME_HANDICAP → 仍返回 *_MODEL_PENDING，由 Worktree 4 接通。
- OTHER → SUBTYPE_UNCLASSIFIED（上游 family 归属误判的 audit 入口）。

evaluator 是纯函数：所有 IO（拉 SeriesState / GameOdds / SeasonSnapshot）已经
由 worker 异步完成并写进 metadata，evaluator 只读快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.sports_season import SeasonSnapshot

from strategies.current._shared.edge_gates import (
    REASON_INSUFFICIENT_EDGE,
    REASON_LIQUIDITY_BELOW_MIN,
    REASON_MISSING_BEST_ASK,
    REASON_PRICE_ABOVE_FAIR,
    check_entry_gates,
)
from strategies.current.series.classifier import classify_series_sub_type
from strategies.current.series.match import series_state_from_metadata
from strategies.current.series.single_game_prob import derive_single_game_prob
from strategies.current.series.team_resolver import resolve_series_team
from strategies.current.series.types import (
    SeriesCandidate,
    SeriesEvaluation,
    SeriesRejectReason,
    SeriesSubType,
)
from strategies.current.series.winner_model import series_win_probability


_GATE_REASON_TO_SERIES: dict[str, SeriesRejectReason] = {
    REASON_MISSING_BEST_ASK: SeriesRejectReason.MISSING_BEST_ASK,
    REASON_LIQUIDITY_BELOW_MIN: SeriesRejectReason.LIQUIDITY_BELOW_MIN,
    REASON_INSUFFICIENT_EDGE: SeriesRejectReason.INSUFFICIENT_EDGE,
    REASON_PRICE_ABOVE_FAIR: SeriesRejectReason.PRICE_ABOVE_FAIR,
}


_SUBTYPE_PENDING_REASON: dict[SeriesSubType, SeriesRejectReason] = {
    SeriesSubType.TOTAL_GAMES: SeriesRejectReason.TOTAL_GAMES_MODEL_PENDING,
    SeriesSubType.GAME_HANDICAP: SeriesRejectReason.HANDICAP_MODEL_PENDING,
}


@dataclass(frozen=True, slots=True)
class SeriesEvaluatorInputs:
    """WINNER 评估所需的运行时上下文（盘口 + 配置）。"""

    best_ask: Decimal | None
    buyable_liquidity_usdc: Decimal
    now: datetime
    min_edge_bps: int
    max_entry_price: Decimal
    min_orderbook_depth_usdc: Decimal
    max_series_state_age_seconds: int
    max_game_odds_age_seconds: int
    season_snapshot: SeasonSnapshot | None


def evaluate_series_opportunity(
    candidate: SeriesCandidate,
    *,
    inputs: SeriesEvaluatorInputs | None = None,
) -> SeriesEvaluation:
    """评估一个 series outcome。

    ``inputs=None`` 是 record-only 模式：只跑 classifier，返回对应
    ``*_MODEL_PENDING`` / ``SUBTYPE_UNCLASSIFIED`` / WINNER 路径上的 MISSING_*
    诊断；不调用模型也不查盘口。生产路径 strategy.py 注入完整 inputs。
    """

    sub_type = classify_series_sub_type(candidate.market)
    if sub_type == SeriesSubType.OTHER:
        return _reject(candidate, sub_type, SeriesRejectReason.SUBTYPE_UNCLASSIFIED)
    if sub_type in _SUBTYPE_PENDING_REASON:
        return _reject(candidate, sub_type, _SUBTYPE_PENDING_REASON[sub_type])
    # WINNER 路径
    return _evaluate_winner(candidate, inputs=inputs)


def _evaluate_winner(
    candidate: SeriesCandidate,
    *,
    inputs: SeriesEvaluatorInputs | None,
) -> SeriesEvaluation:
    sub_type = SeriesSubType.WINNER
    state = series_state_from_metadata(candidate.metadata)
    if state is None:
        return _reject(candidate, sub_type, SeriesRejectReason.MISSING_SERIES_STATE)
    if inputs is None:
        # record-only：缺执行上下文时无法跑模型；保留诊断 reason 但不下盘。
        return _reject(
            candidate,
            sub_type,
            SeriesRejectReason.MISSING_SERIES_ODDS,
            extra_metadata={"series_state_present": True},
        )
    state_age = _age_seconds(state.observed_at, inputs.now)
    if state_age > inputs.max_series_state_age_seconds:
        return _reject(
            candidate,
            sub_type,
            SeriesRejectReason.STALE_SERIES_STATE,
            extra_metadata={"series_state_age_seconds": state_age},
        )
    side = resolve_series_team(candidate.market, candidate.outcome_label, state)
    if side is None:
        return _reject(candidate, sub_type, SeriesRejectReason.SERIES_TEAM_NOT_RESOLVED)
    derived = derive_single_game_prob(
        state=state,
        metadata=candidate.metadata,
        season_snapshot=inputs.season_snapshot,
        now=inputs.now,
        max_game_odds_age_seconds=inputs.max_game_odds_age_seconds,
    )
    if derived is None:
        return _reject(candidate, sub_type, SeriesRejectReason.MISSING_SERIES_ODDS)
    team_a_series_prob = series_win_probability(state, derived.p_a)
    fair_value = team_a_series_prob if side == "team_a" else Decimal(1) - team_a_series_prob
    fair_value = _clamp_price(fair_value)
    gate = check_entry_gates(
        fair_value=fair_value,
        best_ask=inputs.best_ask,
        buyable_liquidity_usdc=inputs.buyable_liquidity_usdc,
        min_edge_bps=inputs.min_edge_bps,
        max_entry_price=inputs.max_entry_price,
        min_orderbook_depth_usdc=inputs.min_orderbook_depth_usdc,
    )
    base_metadata: dict[str, Any] = {
        "series_team_side": side,
        "p_per_game": str(derived.p_a),
        "p_per_game_source": derived.source,
        "team_a_series_prob": str(team_a_series_prob),
        "wins_a": state.wins_a,
        "wins_b": state.wins_b,
        "best_of": state.best_of,
        "series_state_age_seconds": state_age,
    }
    if not gate.passed:
        reason = _GATE_REASON_TO_SERIES[gate.reason or REASON_MISSING_BEST_ASK]
        merged: dict[str, Any] = dict(base_metadata)
        if gate.entry_price_cap is not None:
            merged["entry_price_cap"] = str(gate.entry_price_cap)
        merged.update(gate.metadata)
        return _reject(
            candidate,
            sub_type,
            reason,
            fair_value=fair_value,
            extra_metadata=merged,
        )
    entry_cap = gate.entry_price_cap
    if entry_cap is None:
        raise AssertionError("edge_gates.check_entry_gates returned passed=True without cap")
    accepted_metadata: dict[str, Any] = dict(base_metadata)
    accepted_metadata.update(gate.metadata)
    accepted_metadata["fair_value"] = str(fair_value)
    accepted_metadata["entry_price_cap"] = str(entry_cap)
    return SeriesEvaluation(
        accepted=True,
        sub_type=sub_type,
        candidate=candidate,
        fair_value=fair_value,
        reject_reason=None,
        metadata=accepted_metadata,
    )


def _reject(
    candidate: SeriesCandidate,
    sub_type: SeriesSubType,
    reason: SeriesRejectReason,
    *,
    fair_value: Decimal | None = None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> SeriesEvaluation:
    metadata: dict[str, Any] = {
        "sub_type": sub_type.value,
        "reject_reason": reason.value,
    }
    if fair_value is not None:
        metadata["fair_value"] = str(fair_value)
    if extra_metadata:
        metadata.update(extra_metadata)
    return SeriesEvaluation(
        accepted=False,
        sub_type=sub_type,
        candidate=candidate,
        fair_value=fair_value,
        reject_reason=reason,
        metadata=metadata,
    )


def _clamp_price(value: Decimal) -> Decimal:
    if value < Decimal("0.01"):
        return Decimal("0.01")
    if value > Decimal("0.99"):
        return Decimal("0.99")
    return value


def _age_seconds(observed_at: datetime, now: datetime) -> int:
    a = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    b = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return max(0, int((b - a).total_seconds()))


__all__ = [
    "SeriesEvaluatorInputs",
    "evaluate_series_opportunity",
]
