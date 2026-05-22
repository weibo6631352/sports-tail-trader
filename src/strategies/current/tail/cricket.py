"""板球（cricket）胜负盘扫尾评估。

板球整场胜负是双方对阵 2-way MONEYLINE：哪支球队赢得本场比赛。Goalserve
``cricket/livescore`` 提供每局得分、wickets、以及追分局（2nd innings）的
``required_runs`` / ``required_balls``（来自 comment.post "X need N runs in
M balls"）。

锁定模型（限时赛 T20 / ODI，2 局制）：
- 第 1 局（current_innings < 2）：设定 target 的一方还在打，尚无追分目标，
  结果不可锁定 → ``cricket_not_late_enough``。
- 第 2 局（追分局）：
  - 追分方已追平/超过 target（runs >= target）→ 追分方必胜。
  - 追分方所需 required_runs 超过最多还能拿的分（剩余 balls × 6）→ 追分方
    已不可能达成 → 防守方（首打方）必胜。
  - 追分方全员出局（wickets == 10）且未达 target → 防守方必胜。
- 追分局缺 target / required 数据 → ``cricket_chase_data_missing``，不臆测。

比赛已结束（status=ENDED）由 ``_evaluate_ended_moneyline`` 用 participant.score
（胜者 1 / 负者 0，由 parser 据 winner 字段填充）判定；本模块只处理 LIVE。
"""

from __future__ import annotations

from strategies.sports_framework import (
    SportsMarketSide,
    is_cricket_game,
)

from .core import _accept, _reject
from .types import (
    SportsTailCandidate,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)

# 一局最多 10 个 wicket；第 10 个 wicket 落下即全员出局，该局结束。
_CRICKET_ALL_OUT_WICKETS = 10
# 单球理论最大得分按 6 计（边界），是对"还能拿多少分"的保守上界。
_CRICKET_MAX_RUNS_PER_BALL = 6


def _cricket_market_side(market_side: SportsMarketSide) -> str | None:
    """板球胜负盘是 2-way HOME/AWAY MONEYLINE；其它 side 不支持。"""
    if market_side == SportsMarketSide.HOME:
        return "home"
    if market_side == SportsMarketSide.AWAY:
        return "away"
    return None


def _evaluate_cricket_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """板球胜负盘评估：追分局结果数学锁定时锁定胜方。

    cricket 胜负盘是 2-way MONEYLINE，ask/价格/流动性门禁已在通用
    ``_common_reject_reason`` 完成，本模块只判锁定。
    """
    game = candidate.game
    market = candidate.market
    side = _cricket_market_side(market.side)
    if side is None:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)

    state = game.cricket_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_CRICKET_STATE.value)

    # 第 1 局：设定 target 的一方还在打，尚无追分目标——不可锁定。
    if state.current_innings is None or state.current_innings < 2:
        return _reject(candidate, TailRejectReason.CRICKET_NOT_LATE_ENOUGH.value)

    # 追分局：batting_side 是追分方，对手是防守方（首打方）。
    chasing_side = state.batting_side
    if chasing_side not in ("home", "away"):
        return _reject(candidate, TailRejectReason.CRICKET_CHASE_DATA_MISSING.value)
    defending_side = "away" if chasing_side == "home" else "home"

    # 追分方已追平/超过 target → 追分方必胜（target = 首打方得分 + 1，
    # runs >= target 即超过首打方得分）。
    if state.target is not None and state.runs is not None and state.runs >= state.target:
        return _resolve_cricket(candidate, policy, side, winner=chasing_side, reason="chase_reached")

    # 追分方全员出局且未达 target → 防守方必胜（该局已结束，追分失败）。
    if (
        state.wickets is not None
        and state.wickets >= _CRICKET_ALL_OUT_WICKETS
        and state.target is not None
        and state.runs is not None
        and state.runs < state.target
    ):
        return _resolve_cricket(candidate, policy, side, winner=defending_side, reason="chase_all_out")

    # 追分方所需 required_runs 超过剩余 balls 最多可拿分 → 防守方必胜。
    # required_runs / required_balls 来自 comment.post，是追分局最权威实时信号。
    if state.required_runs is not None and state.required_balls is not None:
        max_obtainable = state.required_balls * _CRICKET_MAX_RUNS_PER_BALL
        if state.required_runs > max_obtainable:
            return _resolve_cricket(
                candidate, policy, side, winner=defending_side, reason="chase_unreachable"
            )
        # required_runs <= 0 表示已达成——防御性归到追分方胜（正常已被上面命中）。
        if state.required_runs <= 0:
            return _resolve_cricket(
                candidate, policy, side, winner=chasing_side, reason="chase_reached"
            )
        # 数据齐全但尚未锁定：追分局进行中，结果仍未定。
        return _reject(candidate, TailRejectReason.CRICKET_OUTCOME_NOT_LOCKED.value)

    # 追分局已开始但缺 target / required 数据——不臆测，给精确可审计原因。
    if state.target is None:
        return _reject(candidate, TailRejectReason.CRICKET_CHASE_DATA_MISSING.value)
    return _reject(candidate, TailRejectReason.CRICKET_OUTCOME_NOT_LOCKED.value)


def _resolve_cricket(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
    market_side: str,
    *,
    winner: str,
    reason: str,
) -> TailEvaluation:
    """已确定胜方时，按盘口方向给出 accept/reject。"""
    if market_side == winner:
        return _accept(
            candidate, f"cricket_moneyline_{reason}", policy.moneyline_execution_permission
        )
    # 盘口方向是输方 → 结果已锁定为对手胜，本盘口不可入场。
    return _reject(candidate, TailRejectReason.CRICKET_OUTCOME_NOT_LOCKED.value)


__all__ = ["_evaluate_cricket_moneyline", "is_cricket_game"]
