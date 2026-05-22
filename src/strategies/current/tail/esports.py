"""电子竞技（esports）胜负盘扫尾评估。

esports（CS2/Dota2/LoL/Valorant）的 Polymarket 胜负盘是双方对阵 2-way
MONEYLINE：哪支战队赢下 best-of-N 系列赛。Goalserve livescore getfeed
``esports/home`` 提供 best-of 局数与各队已赢局数（maps won）。

锁定模型：BO-N 需赢 ``best_of // 2 + 1`` 局即夺冠（BO3→2、BO5→3、BO1→1）。
某方 maps_won 达到该阈值 → 系列赛结果 100% 锁定，该方必胜。比赛结束
（status=ENDED）则直接由 maps_won 较多方判定胜者，走通用 ended-moneyline
评估器；本模块只处理 LIVE 进行中的系列赛锁定判定。
"""

from __future__ import annotations

from strategies.sports_framework import (
    LiveGameState,
    SportsMarketSide,
)

from .core import _accept, _reject
from .types import (
    SportsTailCandidate,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)


def is_esports_game(game: LiveGameState) -> bool:
    """该比赛是否为电子竞技。"""
    return (game.sport or "").strip().lower() == "esports"


def _maps_needed(best_of: int) -> int:
    """BO-N 夺冠所需局数：过半即赢（BO3→2、BO5→3、BO1→1）。"""
    return best_of // 2 + 1


def _evaluate_esports_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """esports 胜负盘评估：某方已赢满系列赛所需局数 → 该方锁定。

    BINARY_PROP 在通用门禁跳过 ask 检查；esports 胜负盘是 2-way MONEYLINE，
    ask/价格/流动性门禁已在通用 ``_common_reject_reason`` 完成，这里只判锁定。
    """
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    state = game.esports_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_ESPORTS_STATE.value)
    # best_of 未知则无法判断夺冠阈值——给可审计原因，不静默放行（§9）。
    if state.best_of is None or state.best_of <= 0:
        return _reject(candidate, TailRejectReason.ESPORTS_BEST_OF_UNKNOWN.value)

    needed = _maps_needed(state.best_of)
    side_maps = (
        state.home_maps_won
        if market.side == SportsMarketSide.HOME
        else state.away_maps_won
    )
    if side_maps >= needed:
        return _accept(
            candidate,
            "esports_moneyline_locked",
            policy.moneyline_execution_permission,
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)
