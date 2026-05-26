"""Series 评估器。

按 sub_type 分派：
- WINNER → ``series_win_probability(state, p_per_game)`` 作为 fair_value。
- TOTAL_GAMES → 总场数分布 + Over/Under 累加；fair_value 由方向决定。
- GAME_HANDICAP → series-scope 走 ``series_handicap_cover_probability``；
  single_game-scope 走 ``single_game_cover_probability`` (需 game_spreads)。
- OTHER → SUBTYPE_UNCLASSIFIED（上游 family 归属误判的 audit 入口）。

evaluator 是纯函数：所有 IO（拉 SeriesState / GameOdds / SeasonSnapshot / GameSpreads）
已经由 worker 异步完成并写进 metadata，evaluator 只读快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.sports_season import SeasonSnapshot

from polymarket_trader.workflow._shared.edge_gates import (
    REASON_INSUFFICIENT_EDGE,
    REASON_LIQUIDITY_BELOW_MIN,
    REASON_MISSING_BEST_ASK,
    REASON_PRICE_ABOVE_FAIR,
    check_entry_gates,
    implied_mid_probability,
)
from polymarket_trader.workflow.series.classifier import classify_series_sub_type
from polymarket_trader.workflow.series.handicap_model import (
    series_handicap_cover_probability,
    single_game_cover_probability,
)
from polymarket_trader.workflow.series.handicap_outcome import (
    HandicapBet,
    parse_handicap_outcome,
)
from polymarket_trader.workflow.series.match import (
    game_spreads_from_metadata,
    series_state_from_metadata,
)
from polymarket_trader.workflow.series.single_game_prob import (
    SingleGameProb,
    derive_single_game_prob,
)
from polymarket_trader.workflow.series.team_resolver import resolve_series_team
from polymarket_trader.workflow.series.total_games_model import (
    prob_over,
    prob_under,
    push_probability,
    total_games_distribution,
)
from polymarket_trader.workflow.series.total_games_outcome import parse_total_games_outcome
from polymarket_trader.workflow.series.types import (
    SeriesCandidate,
    SeriesEvaluation,
    SeriesRejectReason,
    SeriesState,
    SeriesSubType,
)
from polymarket_trader.workflow.series.winner_model import series_win_probability


_GATE_REASON_TO_SERIES: dict[str, SeriesRejectReason] = {
    REASON_MISSING_BEST_ASK: SeriesRejectReason.MISSING_BEST_ASK,
    REASON_LIQUIDITY_BELOW_MIN: SeriesRejectReason.LIQUIDITY_BELOW_MIN,
    REASON_INSUFFICIENT_EDGE: SeriesRejectReason.INSUFFICIENT_EDGE,
    REASON_PRICE_ABOVE_FAIR: SeriesRejectReason.PRICE_ABOVE_FAIR,
}


@dataclass(frozen=True, slots=True)
class SeriesEvaluatorInputs:
    """评估所需的运行时上下文（盘口 + 配置）。

    ``min_edge_bps`` / ``max_entry_price`` / ``min_orderbook_depth_usdc`` 按 sub_type
    由 strategy 注入对应配置项（WINNER / TOTAL_GAMES / HANDICAP 互不挤占）。
    """

    best_ask: Decimal | None
    best_bid: Decimal | None
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

    ``inputs=None`` 是 record-only 模式：只跑 classifier + state 检查，返回对应
    诊断 reason；不调用模型也不查盘口。生产路径 strategy.py 注入完整 inputs。
    """

    sub_type = classify_series_sub_type(candidate.market)
    if sub_type == SeriesSubType.OTHER:
        return _reject(candidate, sub_type, SeriesRejectReason.SUBTYPE_UNCLASSIFIED)
    if sub_type == SeriesSubType.WINNER:
        return _evaluate_winner(candidate, inputs=inputs)
    if sub_type == SeriesSubType.TOTAL_GAMES:
        return _evaluate_total_games(candidate, inputs=inputs)
    return _evaluate_handicap(candidate, inputs=inputs)


def _evaluate_winner(
    candidate: SeriesCandidate,
    *,
    inputs: SeriesEvaluatorInputs | None,
) -> SeriesEvaluation:
    sub_type = SeriesSubType.WINNER
    common = _prepare_common(candidate, sub_type=sub_type, inputs=inputs)
    if common.rejection is not None:
        return common.rejection
    state = common.state
    derived = common.derived
    side = resolve_series_team(candidate.market, candidate.outcome_label, state)
    if side is None:
        return _reject(candidate, sub_type, SeriesRejectReason.SERIES_TEAM_NOT_RESOLVED)
    team_a_series_prob = series_win_probability(state, derived.p_a)
    fair_value = team_a_series_prob if side == "team_a" else Decimal(1) - team_a_series_prob
    fair_value = _clamp_price(fair_value)
    base_metadata: dict[str, Any] = {
        "series_team_side": side,
        "p_per_game": str(derived.p_a),
        "p_per_game_source": derived.source,
        "team_a_series_prob": str(team_a_series_prob),
        "wins_a": state.wins_a,
        "wins_b": state.wins_b,
        "best_of": state.best_of,
        "series_state_age_seconds": common.state_age,
    }
    return _gate_and_pack(candidate, sub_type, fair_value, base_metadata, inputs=common.inputs)


def _evaluate_total_games(
    candidate: SeriesCandidate,
    *,
    inputs: SeriesEvaluatorInputs | None,
) -> SeriesEvaluation:
    sub_type = SeriesSubType.TOTAL_GAMES
    parsed = parse_total_games_outcome(candidate.outcome_label, candidate.market.market_question)
    if parsed is None:
        return _reject(candidate, sub_type, SeriesRejectReason.SERIES_OUTCOME_NOT_PARSED)
    line, direction = parsed
    common = _prepare_common(candidate, sub_type=sub_type, inputs=inputs)
    if common.rejection is not None:
        return common.rejection
    state = common.state
    derived = common.derived
    distribution = total_games_distribution(state, derived.p_a)
    if direction == "over":
        fair_value = prob_over(distribution, line)
    else:
        fair_value = prob_under(distribution, line)
    fair_value = _clamp_price(fair_value)
    base_metadata: dict[str, Any] = {
        "total_games_line": str(line),
        "total_games_direction": direction,
        "p_per_game": str(derived.p_a),
        "p_per_game_source": derived.source,
        "push_probability": str(push_probability(distribution, line)),
        "wins_a": state.wins_a,
        "wins_b": state.wins_b,
        "best_of": state.best_of,
        "series_state_age_seconds": common.state_age,
    }
    return _gate_and_pack(candidate, sub_type, fair_value, base_metadata, inputs=common.inputs)


def _evaluate_handicap(
    candidate: SeriesCandidate,
    *,
    inputs: SeriesEvaluatorInputs | None,
) -> SeriesEvaluation:
    sub_type = SeriesSubType.GAME_HANDICAP
    state_only = series_state_from_metadata(candidate.metadata)
    if state_only is None:
        return _reject(candidate, sub_type, SeriesRejectReason.MISSING_SERIES_STATE)
    bet = parse_handicap_outcome(candidate.outcome_label, candidate.market, state_only)
    if bet is None:
        return _reject(candidate, sub_type, SeriesRejectReason.SERIES_OUTCOME_NOT_PARSED)
    if bet.scope == "single_game":
        return _evaluate_handicap_single_game(candidate, bet=bet, state=state_only, inputs=inputs)
    return _evaluate_handicap_series(candidate, bet=bet, inputs=inputs)


def _evaluate_handicap_series(
    candidate: SeriesCandidate,
    *,
    bet: HandicapBet,
    inputs: SeriesEvaluatorInputs | None,
) -> SeriesEvaluation:
    sub_type = SeriesSubType.GAME_HANDICAP
    common = _prepare_common(candidate, sub_type=sub_type, inputs=inputs)
    if common.rejection is not None:
        return common.rejection
    state = common.state
    derived = common.derived
    # series_handicap_cover_probability 以 team_a 视角；若 bet 针对 team_b，
    # 直接用对称：team_b covers ⟺ team_a 输 > handicap_a，即镜像 line。
    if bet.team_side == "team_a":
        fair_value = series_handicap_cover_probability(state, derived.p_a, bet.handicap)
    else:
        # team_b covers H ⟺ final_b - final_a > H ⟺ final_a - final_b < -H
        # team_a 视角 handicap = -H 时，team_a 覆盖（diff > -H）的补集是 team_b 覆盖。
        team_a_cover = series_handicap_cover_probability(state, derived.p_a, -bet.handicap)
        fair_value = Decimal(1) - team_a_cover
    fair_value = _clamp_price(fair_value)
    base_metadata: dict[str, Any] = {
        "handicap_scope": "series",
        "handicap_team_side": bet.team_side,
        "handicap_line": str(bet.handicap),
        "p_per_game": str(derived.p_a),
        "p_per_game_source": derived.source,
        "wins_a": state.wins_a,
        "wins_b": state.wins_b,
        "best_of": state.best_of,
        "series_state_age_seconds": common.state_age,
    }
    return _gate_and_pack(candidate, sub_type, fair_value, base_metadata, inputs=common.inputs)


def _evaluate_handicap_single_game(
    candidate: SeriesCandidate,
    *,
    bet: HandicapBet,
    state: SeriesState,
    inputs: SeriesEvaluatorInputs | None,
) -> SeriesEvaluation:
    sub_type = SeriesSubType.GAME_HANDICAP
    if inputs is None:
        return _reject(
            candidate,
            sub_type,
            SeriesRejectReason.MISSING_GAME_SPREADS,
            extra_metadata={"handicap_scope": "single_game"},
        )
    state_age = _age_seconds(state.observed_at, inputs.now)
    if state_age > inputs.max_series_state_age_seconds:
        return _reject(
            candidate,
            sub_type,
            SeriesRejectReason.STALE_SERIES_STATE,
            extra_metadata={
                "series_state_age_seconds": state_age,
                "handicap_scope": "single_game",
            },
        )
    spreads = game_spreads_from_metadata(candidate.metadata)
    if spreads is None:
        return _reject(
            candidate,
            sub_type,
            SeriesRejectReason.MISSING_GAME_SPREADS,
            extra_metadata={"handicap_scope": "single_game"},
        )
    spread_age = _age_seconds(spreads.observed_at, inputs.now)
    if spread_age > inputs.max_game_odds_age_seconds:
        return _reject(
            candidate,
            sub_type,
            SeriesRejectReason.STALE_GAME_SPREADS,
            extra_metadata={
                "spreads_age_seconds": spread_age,
                "handicap_scope": "single_game",
            },
        )
    target_team = state.team_a if bet.team_side == "team_a" else state.team_b
    p_cover = single_game_cover_probability(
        spreads,
        team_a=target_team,
        handicap=bet.handicap,
    )
    if p_cover is None:
        return _reject(
            candidate,
            sub_type,
            SeriesRejectReason.MISSING_GAME_SPREADS,
            extra_metadata={
                "handicap_scope": "single_game",
                "spreads_line_mismatch": str(spreads.spread_line),
                "bet_line": str(bet.handicap),
            },
        )
    fair_value = _clamp_price(p_cover)
    base_metadata: dict[str, Any] = {
        "handicap_scope": "single_game",
        "handicap_team_side": bet.team_side,
        "handicap_line": str(bet.handicap),
        "spreads_line": str(spreads.spread_line),
        "spreads_source": spreads.source,
        "spreads_age_seconds": spread_age,
        "wins_a": state.wins_a,
        "wins_b": state.wins_b,
        "best_of": state.best_of,
        "series_state_age_seconds": state_age,
    }
    return _gate_and_pack(candidate, sub_type, fair_value, base_metadata, inputs=inputs)


@dataclass(frozen=True, slots=True)
class _CommonInputs:
    """WINNER / TOTAL_GAMES / series-scope HANDICAP 共享的输入收集结果。

    accept 路径下 ``rejection`` 为 None，其余字段非 None；reject 路径下
    ``rejection`` 为携带原因的 SeriesEvaluation，其余字段未必有效。
    """

    rejection: SeriesEvaluation | None
    state: SeriesState
    derived: SingleGameProb
    state_age: int
    inputs: SeriesEvaluatorInputs


_INVALID_STATE = SeriesState(
    team_a="",
    team_b="",
    wins_a=0,
    wins_b=0,
    best_of=1,
    next_game_at=None,
    observed_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
)
_INVALID_DERIVED = SingleGameProb(p_a=Decimal("0.5"), source="invalid")


def _prepare_common(
    candidate: SeriesCandidate,
    *,
    sub_type: SeriesSubType,
    inputs: SeriesEvaluatorInputs | None,
) -> _CommonInputs:
    state = series_state_from_metadata(candidate.metadata)
    if state is None:
        return _CommonInputs(
            rejection=_reject(candidate, sub_type, SeriesRejectReason.MISSING_SERIES_STATE),
            state=_INVALID_STATE,
            derived=_INVALID_DERIVED,
            state_age=0,
            inputs=_FAKE_INPUTS,
        )
    if inputs is None:
        return _CommonInputs(
            rejection=_reject(
                candidate,
                sub_type,
                SeriesRejectReason.MISSING_SERIES_ODDS,
                extra_metadata={"series_state_present": True},
            ),
            state=state,
            derived=_INVALID_DERIVED,
            state_age=0,
            inputs=_FAKE_INPUTS,
        )
    state_age = _age_seconds(state.observed_at, inputs.now)
    if state_age > inputs.max_series_state_age_seconds:
        return _CommonInputs(
            rejection=_reject(
                candidate,
                sub_type,
                SeriesRejectReason.STALE_SERIES_STATE,
                extra_metadata={"series_state_age_seconds": state_age},
            ),
            state=state,
            derived=_INVALID_DERIVED,
            state_age=state_age,
            inputs=inputs,
        )
    derived = derive_single_game_prob(
        state=state,
        metadata=candidate.metadata,
        season_snapshot=inputs.season_snapshot,
        now=inputs.now,
        max_game_odds_age_seconds=inputs.max_game_odds_age_seconds,
    )
    if derived is None:
        return _CommonInputs(
            rejection=_reject(candidate, sub_type, SeriesRejectReason.MISSING_SERIES_ODDS),
            state=state,
            derived=_INVALID_DERIVED,
            state_age=state_age,
            inputs=inputs,
        )
    return _CommonInputs(
        rejection=None,
        state=state,
        derived=derived,
        state_age=state_age,
        inputs=inputs,
    )


_FAKE_INPUTS = SeriesEvaluatorInputs(
    best_ask=None,
    best_bid=None,
    buyable_liquidity_usdc=Decimal("0"),
    now=datetime(1970, 1, 1, tzinfo=timezone.utc),
    min_edge_bps=0,
    max_entry_price=Decimal("0.99"),
    min_orderbook_depth_usdc=Decimal("0"),
    max_series_state_age_seconds=0,
    max_game_odds_age_seconds=0,
    season_snapshot=None,
)


def _gate_and_pack(
    candidate: SeriesCandidate,
    sub_type: SeriesSubType,
    fair_value: Decimal,
    base_metadata: dict[str, Any],
    *,
    inputs: SeriesEvaluatorInputs,
) -> SeriesEvaluation:
    gate = check_entry_gates(
        fair_value=fair_value,
        best_ask=inputs.best_ask,
        buyable_liquidity_usdc=inputs.buyable_liquidity_usdc,
        min_edge_bps=inputs.min_edge_bps,
        max_entry_price=inputs.max_entry_price,
        min_orderbook_depth_usdc=inputs.min_orderbook_depth_usdc,
    )
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
    implied = implied_mid_probability(inputs.best_bid, inputs.best_ask)
    if implied is not None:
        accepted_metadata["implied_mid_prob"] = str(implied)
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
