"""橄榄球（rugby）胜负盘评估。

橄榄球 Polymarket 盘口为 3-way 胜/平/负，拆成 3 个 Yes/No binary_prop——slug
后缀为主队缩写 / ``draw`` / 客队缩写。胜负在比分差足够大且临近终场时锁定；
橄榄球计分 converted try=7、penalty/drop goal=3，故"安全领先"按剩余时间缩放：
越接近终场对手越难追回，所需领先越小。
"""

from __future__ import annotations

from strategies.sports_framework import (
    LiveGameState,
    SportsMarketSide,
    SportsMarketSnapshot,
)

from .core import _accept, _reject
from .types import (
    SportsTailCandidate,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)

# (剩余分钟上限, 锁定所需领先) —— 按剩余时间从小到大匹配。
_RUGBY_MARGIN_BY_MINUTES_LEFT: tuple[tuple[float, int], ...] = (
    (5.0, 9),    # ≤5 分钟：领先 ≥9（对手需 2 次得分才可能反超）
    (12.0, 15),  # ≤12 分钟：领先 ≥15
    (20.0, 22),  # ≤20 分钟：领先 ≥22（约 3 次 converted try）
)


def is_rugby_game(game: LiveGameState) -> bool:
    """该比赛是否为橄榄球。"""
    return (game.sport or "").strip().lower() == "rugby"


def _rugby_required_margin(seconds_remaining: int) -> int | None:
    """按剩余时间返回锁定所需领先；超过 20 分钟（太早）返回 None。"""
    minutes_left = seconds_remaining / 60.0
    for cutoff, margin in _RUGBY_MARGIN_BY_MINUTES_LEFT:
        if minutes_left <= cutoff:
            return margin
    return None


def _rugby_market_direction(market: SportsMarketSnapshot) -> str | None:
    """从 slug 解析胜负方向。

    slug 形如 ``{league}-{home}-{away}-{yyyy}-{mm}-{dd}-{suffix}``：
    suffix == 主队缩写 → ``home``；== 客队缩写 → ``away``；== ``draw`` → ``draw``。
    """
    slug = (market.market_slug or "").strip().lower()
    parts = [p for p in slug.split("-") if p]
    if len(parts) < 7:
        return None
    home_abbr, away_abbr, suffix = parts[1], parts[2], parts[-1]
    if suffix == "draw":
        return "draw"
    if suffix == home_abbr:
        return "home"
    if suffix == away_abbr:
        return "away"
    return None


def _evaluate_rugby_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """橄榄球胜负盘评估：领先方在临近终场达到安全分差时锁定。"""

    game = candidate.game
    market = candidate.market
    direction = _rugby_market_direction(market)
    if direction is None:
        return _reject(candidate, TailRejectReason.RUGBY_MARKET_NOT_SUPPORTED.value)
    # 平局盘无可靠的盘中锁定模型——给可审计原因后跳过自动执行（§17 record-only）。
    if direction == "draw":
        return _reject(candidate, TailRejectReason.RUGBY_DRAW_NOT_SUPPORTED.value)
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if game.seconds_remaining is None:
        return _reject(candidate, TailRejectReason.MISSING_SECONDS_REMAINING.value)
    required = _rugby_required_margin(game.seconds_remaining)
    if required is None:
        return _reject(candidate, TailRejectReason.RUGBY_NOT_LATE_ENOUGH.value)
    # direction 方相对对手的领先（正=领先，负=落后）。
    if direction == "home":
        lead = game.home_score - game.away_score
    else:
        lead = game.away_score - game.home_score
    if market.side == SportsMarketSide.YES:
        # "X 会赢" 的 YES：X 领先达到安全分差 → 锁定。
        if lead >= required:
            return _accept(
                candidate, "rugby_moneyline_locked", policy.moneyline_execution_permission
            )
        return _reject(candidate, TailRejectReason.RUGBY_LEAD_NOT_SAFE.value)
    # "X 会赢" 的 NO：X 落后达到安全分差 → X 已不可能赢（最多打平）→ NO 锁定。
    if -lead >= required:
        return _accept(
            candidate, "rugby_moneyline_no_locked", policy.moneyline_execution_permission
        )
    return _reject(candidate, TailRejectReason.RUGBY_LEAD_NOT_SAFE.value)
