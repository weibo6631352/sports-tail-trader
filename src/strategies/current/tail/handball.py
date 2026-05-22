"""手球（handball）胜负盘扫尾评估。

手球整场胜负是双方对阵 2-way MONEYLINE。Goalserve ``handball/live`` 与
``handball/home`` 内容完全一致：只给 ``status``（"1st Half" / "2nd Half" /
"Finished"）、``totalscore`` 全场比分、``t1`` / ``t2`` 半场比分——**没有任何
盘中时钟字段**（feed 里的 ``time`` 是开球时刻 HH:MM，不是比赛进行时间）。

因为没有剩余时间，无法做"按时间缩放安全分差"的精确锁定模型（不同于
rugby / soccer 有 timer）。诚实建模只能依赖两个无需时钟的信号：

1. 比赛已结束（status=ENDED）→ 由 ``_evaluate_ended_moneyline`` 用最终比分
   判定，不进本模块（本模块只处理 LIVE）。
2. 盘中"超大领先"——手球节奏约每队每分钟 1 球，下半场出现 12+ 球净胜本质
   上不可逆转（这种规模的逆转在手球里极罕见）；缺半场信息时要求更大的
   16+ 球领先。这是按数据真实支持的程度建模，不臆造时钟。

其余情形（领先不够大 / 缺状态）一律给精确可审计拒绝原因。
"""

from __future__ import annotations

from strategies.sports_framework import (
    SportsMarketSide,
    is_handball_game,
)

from .core import _accept, _reject
from .types import (
    SportsTailCandidate,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)

# 下半场锁定所需净胜球：12 球。手球每队每分钟约 1 球，下半场（≤30 分钟）
# 追回 12 球需要对手单边净胜 12，实战中极罕见——是无时钟下的保守安全阈值。
_HANDBALL_SECOND_HALF_SAFE_LEAD = 12
# 缺半场信息（period 未知）时要求更大的领先：16 球。无法判断处于上/下半场，
# 用更保守的阈值兜底，避免上半场早期大比分被误判锁定。
_HANDBALL_NO_PERIOD_SAFE_LEAD = 16


def _evaluate_handball_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """手球胜负盘评估：无盘中时钟，只对超大领先（或已判定）锁定。

    手球胜负盘是 2-way MONEYLINE，ask/价格/流动性门禁已在通用
    ``_common_reject_reason`` 完成，本模块只判锁定。
    """
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)

    # period 来自 handball_state；缺 state 时仍可用 status 文本回退判断半场。
    state = game.handball_state
    period = state.period if state is not None else None

    # 锁定所需净胜球：下半场 12，半场未知 16。上半场（first_half）一律不锁定——
    # 还有完整下半场，任何盘中领先都可能被逆转。
    if period == "second_half":
        required_lead = _HANDBALL_SECOND_HALF_SAFE_LEAD
    elif period == "first_half":
        return _reject(candidate, TailRejectReason.HANDBALL_LEAD_NOT_SAFE.value)
    elif period is None:
        # period 未知——可能是 state 缺失或数据源未给赛段。用最保守阈值兜底，
        # 不臆造赛段。
        required_lead = _HANDBALL_NO_PERIOD_SAFE_LEAD
    else:
        # extra_time 等其它赛段：同样用最保守阈值，不专门建模。
        required_lead = _HANDBALL_NO_PERIOD_SAFE_LEAD

    lead = game.score_diff_for(market.side)
    if lead >= required_lead:
        return _accept(
            candidate, "handball_moneyline_large_lead", policy.moneyline_execution_permission
        )
    return _reject(candidate, TailRejectReason.HANDBALL_LEAD_NOT_SAFE.value)


__all__ = ["_evaluate_handball_moneyline", "is_handball_game"]
