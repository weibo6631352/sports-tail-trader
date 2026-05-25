"""体育扫尾评估的公开入口与体育分派。

包含：
- 公开评估函数 ``evaluate_tail_opportunity`` / ``evaluate_scale_in_opportunity``
- 体育分派（按联赛选 generic / MLB / NFL / Tennis 评估器）
- 通用入场门槛检查（``_common_reject_reason`` / ``_market_data_reject_reason``）
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from decimal import Decimal

from strategies.sports_framework import (
    LiveGameState,
    LiveGameStatus,
    SportsMarketScopeType,
    SportsMarketSnapshot,
    SportsMarketType,
    is_combat_sport_game,
    is_cricket_game,
    is_handball_game,
    is_hockey_game,
    is_mlb_game,
    is_nfl_game,
    is_soccer_game,
    is_tennis_game,
    market_scope,
)

from .core import (
    _accept,
    _candidate,
    _evaluate_basketball_first_half,
    _evaluate_basketball_quarter,
    _evaluate_basketball_second_half,
    _evaluate_ended_moneyline,
    _evaluate_ended_spreads,
    _evaluate_ended_totals,
    _evaluate_moneyline,
    _evaluate_moneyline_scale_in,
    _evaluate_soccer_btts,
    _evaluate_soccer_exact_score,
    _evaluate_soccer_halftime_result,
    _evaluate_soccer_moneyline,
    _evaluate_spreads,
    _evaluate_spreads_scale_in,
    _evaluate_totals,
    _evaluate_totals_scale_in,
    _market_family_reject_reason,
    _reject,
    is_soccer_btts_market,
    is_soccer_exact_score_market,
    is_soccer_halftime_market,
    is_soccer_moneyline_market,
)
from .mlb import (
    _evaluate_mlb_moneyline,
    _evaluate_mlb_nrfi,
    _evaluate_mlb_spreads,
    _evaluate_mlb_totals,
    is_nrfi_market,
)
from .anytime_goalscorer import (
    _evaluate_anytime_goalscorer,
    is_anytime_goalscorer_market,
)
from .esports import _evaluate_esports_moneyline, is_esports_game
from .event_props import (
    evaluate_event_prop,
    event_prop_reject_reason,
    is_modeled_event_prop_market,
)
from .cricket import _evaluate_cricket_moneyline
from .handball import _evaluate_handball_moneyline
from .rugby import _evaluate_rugby_moneyline, is_rugby_game
from .sport_props import sport_specific_prop_reject_reason
from .slug import (
    _is_tennis_set_handicap_market,
    _is_tennis_set_winner_market,
    _market_scope_reject_reason,
)
from .tennis import (
    _evaluate_ended_tennis,
    _evaluate_tennis_moneyline,
    _evaluate_tennis_scale_in,
    _evaluate_tennis_set_handicap,
    _evaluate_tennis_totals,
    _tennis_set_winner_completed_for_side,
)
from .types import (
    ExecutionPermission,
    SportsTailCandidate,
    SportsTailOpportunityType,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)


def _entry_math_lock_veto(candidate: SportsTailCandidate) -> str | None:
    """所有 entry path 的最终一道闸门：math_lock 一票否决"已输方向"。

    任何 evaluator（lockin / event_prop / 体育专属）在 ACCEPT 前都必须
    经过这道闸门——只要 math_lock 公式判定当前 token 是"已输方向"
    （lock_probability ≤ 0.05 且 reason 含"输方"关键词），统一 veto。

    设计原则：
    - unsupported（公式未覆盖的盘口）→ 放行，遵守 §17 不放过可盈利市场
    - math_lock 覆盖且明确为输方 → veto，避免重蹈 Kalinina first-set-winner 损失
    - 其它情况（leading / 未结 / 缺数据）→ 放行
    """
    from strategies.sports_framework.math_lock import evaluate_math_lock

    market = candidate.market
    lock = evaluate_math_lock(
        market.market_type, market.side, market.line, candidate.game,
        market_slug=market.market_slug,
    )
    if lock.method == "unsupported":
        return None
    # task #38 修复后 sub-scope dispatch 不再错走整场公式（无专用公式的子段返回
    # unsupported 而非错算 lock_prob=0），结算性 keyword 现在安全：公式正确时
    # 赢方 lock_prob=1 不被 veto、输方 lock_prob=0 被 veto。恢复完整 keyword
    # 列表以增强保护。如未来发现新公式 bug 导致赢方 lock_prob<0.05，应修公式
    # （veto 反向缩窄是治标）。
    LOSER_KEYWORDS = (
        "already_lost",
        "already_exceeded_line_lose",
        "already_over_lose",
        "match_already_lost",
        "set_already_lost",
        "no_remaining_half_innings",
        "no_remaining_time",
        "no_remaining_balls_or_wickets",
        "chase_target_reached",
        "run_already_scored_in_first",
        "halftime_settled",
        "first_inning_completed",
        "quarter_ended",
        "match_ended",
        "game_already_ended",
    )
    if (
        lock.lock_probability <= Decimal("0.05")
        and any(kw in lock.reason for kw in LOSER_KEYWORDS)
    ):
        return f"math_lock_veto_loser_side:{lock.method}:{lock.reason}"
    return None


def evaluate_tail_opportunity(
    game: LiveGameState | None,
    market: SportsMarketSnapshot,
    *,
    policy: TailPolicy,
    now: datetime | None = None,
) -> TailEvaluation:
    """评估体育盘口是否构成扫尾机会。"""

    family_reject_reason = _market_family_reject_reason(market.market_family)
    if family_reject_reason is not None:
        return _reject(
            None,
            family_reject_reason.value,
            metadata={
                "market_family": market.market_family.value,
                "market_type": market.market_type.value,
                "side": market.side.value,
                "line": str(market.line) if market.line is not None else None,
                "best_ask": str(market.best_ask) if market.best_ask is not None else None,
            },
        )

    if game is None:
        return _reject(None, TailRejectReason.MISSING_LIVE_GAME_STATE.value)

    candidate = _candidate(game, market)
    scope_reject_reason = _market_scope_reject_reason(market)
    if scope_reject_reason is not None:
        return _reject(candidate, scope_reject_reason.value)
    if game.status == LiveGameStatus.ENDED:
        market_reject_reason = _market_data_reject_reason(game, market, policy)
        if market_reject_reason:
            return _reject(candidate, market_reject_reason.value)
        return _evaluate_ended_not_closed(candidate, policy)

    common_reject_reason = _common_reject_reason(game, market, policy, now=now)
    if common_reject_reason:
        return _reject(candidate, common_reject_reason.value)

    # 通用入场守卫：盘口风向单边下杀直接拒。
    from decimal import Decimal as _Dec

    ob_dir = market.metadata.get("orderbook_direction") if isinstance(market.metadata, dict) else None
    if isinstance(ob_dir, dict):
        label = str(ob_dir.get("direction_label") or "")
        try:
            confidence = _Dec(str(ob_dir.get("confidence") or "0"))
        except (ArithmeticError, ValueError, TypeError):
            confidence = _Dec("0")
        if label == "no" and confidence >= _Dec("0.5"):
            return _reject(
                candidate,
                TailRejectReason.ORDERBOOK_DIRECTION_BEARISH.value,
                metadata={
                    "direction_label": label,
                    "confidence": str(confidence),
                    "direction_score": str(ob_dir.get("direction_score") or ""),
                    "flow_imbalance": str(ob_dir.get("flow_imbalance") or ""),
                    "guard_scope": "tail_evaluator_top",
                },
            )

    # 扫尾锁定（结果数学锁定的确定性入场）——唯一入场路径。
    locked_evaluation = _dispatch_tail_lock(game, market, candidate, policy)
    if locked_evaluation.accepted:
        veto_reason = _entry_math_lock_veto(candidate)
        if veto_reason is not None:
            return _reject(candidate, veto_reason)
    return locked_evaluation


def _dispatch_tail_lock(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """按运动/盘口分派扫尾锁定评估（赔率差价之外的原确定性入场路径）。"""

    # 运动专属 prop 家族（拳击/MMA 胜利方式、F1 子盘口、板球 prop）：用 Gamma
    # sportsMarketType 精确识别，给 distinct 可审计拒绝原因（CLAUDE.md §17）。
    # 必须先于 scope / 体育专属分派——这些 prop 的 market_type 可能是 MONEYLINE
    # （多结果胜利方式）或 BINARY_PROP（Yes/No F1 子盘口），不先拦截会被泛化的
    # binary_prop_no_tail_model / OUTCOME_NOT_LOCKED 兜底吞掉具体原因。这三类
    # 都不可扫尾锁定、也无所需专属数据源，故只给精确原因、不建模。
    sport_prop_reject = sport_specific_prop_reject_reason(market)
    if sport_prop_reject is not None:
        return _reject(candidate, sport_prop_reject.value)

    # 足球 anytime-goalscorer：BINARY_PROP 但锁定模型基于球员级 goal_events，
    # 完全独立于扫尾/分段/事件 prop 链路；必须先于 scope / 体育专属 binary_prop
    # 分派，否则会被 soccer 整场 BTTS / exact-score / moneyline 误吞或落到
    # 兜底 binary_prop_no_tail_model。
    if is_anytime_goalscorer_market(market):
        return _evaluate_anytime_goalscorer(candidate, policy)

    # 分段盘口（半场/单节/分节）：先按 scope 分派到分段评估器，避免落到整场
    # sport 评估器后被误判为缺数据。
    scope_type = market_scope(market).scope_type
    if scope_type == SportsMarketScopeType.BASKETBALL_FIRST_HALF:
        return _evaluate_basketball_first_half(candidate, policy)
    if scope_type == SportsMarketScopeType.BASKETBALL_QUARTER:
        return _evaluate_basketball_quarter(candidate, policy)
    if scope_type == SportsMarketScopeType.BASKETBALL_SECOND_HALF:
        return _evaluate_basketball_second_half(candidate, policy)
    # 已识别为分段 ML/spread 但该运动无干净分段模型（冰球分节、棒球 F5 等）——
    # 给出精确可审计拒绝原因，区别于误导性的 missing_*_state 数据缺失原因。
    if scope_type == SportsMarketScopeType.UNSUPPORTED_SUBPERIOD:
        reason = (
            TailRejectReason.UNSUPPORTED_PERIOD_SPREAD
            if market.market_type == SportsMarketType.SPREADS
            else TailRejectReason.UNSUPPORTED_PERIOD_MONEYLINE
        )
        return _reject(candidate, reason.value)

    # 利基事件型 prop（零封/race-to-N/平局退款/双重机会/总分奇偶/净胜分桶/
    # 首得分方）跨运动通用（race-to-N 见于篮球、odd/even 见于棒球等）。先于
    # MLB / rugby 等体育专属 binary_prop 分支识别，避免被它们的
    # UNSUPPORTED_MARKET_TYPE 兜底吞掉具体的可审计原因（CLAUDE.md §17）。
    # NRFI / 足球 BTTS / 半场赛果 / 精确比分 / 胜负盘的 slug 标记与本组互不
    # 重叠，不受影响。
    if market.market_type == SportsMarketType.BINARY_PROP:
        precise_reject = event_prop_reject_reason(market)
        if precise_reject is not None:
            return _reject(candidate, precise_reject.value)
        if is_modeled_event_prop_market(market):
            return evaluate_event_prop(candidate, policy)

    if is_mlb_game(game):
        if market.market_type == SportsMarketType.TOTALS:
            return _evaluate_mlb_totals(candidate, policy)
        if market.market_type == SportsMarketType.MONEYLINE:
            return _evaluate_mlb_moneyline(candidate, policy)
        if market.market_type == SportsMarketType.SPREADS:
            return _evaluate_mlb_spreads(candidate, policy)
        if market.market_type == SportsMarketType.BINARY_PROP and is_nrfi_market(market):
            return _evaluate_mlb_nrfi(candidate, policy)
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)

    if is_nfl_game(game):
        return _evaluate_nfl_manual_review(candidate, policy)

    if market.market_type == SportsMarketType.BINARY_PROP and is_soccer_halftime_market(market):
        return _evaluate_soccer_halftime_result(candidate, policy)

    if market.market_type == SportsMarketType.BINARY_PROP and is_soccer_btts_market(market):
        return _evaluate_soccer_btts(candidate, policy)

    # 橄榄球胜负盘是 3-way 拆成的 binary_prop——路由到橄榄球评估器。
    if market.market_type == SportsMarketType.BINARY_PROP and is_rugby_game(game):
        return _evaluate_rugby_moneyline(candidate, policy)

    # 足球胜负盘也是 3-way 拆成的 binary_prop——排在 rugby 之后，避免橄榄球
    # 同构 slug 误入；再以 is_soccer_game 收口。
    if (
        market.market_type == SportsMarketType.BINARY_PROP
        and is_soccer_game(game)
        and is_soccer_moneyline_market(market)
    ):
        return _evaluate_soccer_moneyline(candidate, policy)

    if market.market_type == SportsMarketType.BINARY_PROP and is_soccer_exact_score_market(market):
        return _evaluate_soccer_exact_score(candidate, policy)

    if market.market_type == SportsMarketType.BINARY_PROP:
        # 利基事件型 prop 已在分派开头识别处理；走到这里的 binary_prop 既不属于
        # 任何体育专属 prop（NRFI/BTTS/半场/精确比分/胜负盘），也不属于已建模
        # 或精确拒绝的事件 prop——仍无专用模型，给泛化兜底原因。
        return _reject(candidate, "binary_prop_no_tail_model")

    # esports 胜负盘是 2-way MONEYLINE，但锁定模型是 best-of 局数而非比分/剩余时间——
    # 必须在通用 _evaluate_moneyline 之前路由到 esports 专属评估器。
    if is_esports_game(game):
        if market.market_type == SportsMarketType.MONEYLINE:
            return _evaluate_esports_moneyline(candidate, policy)
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)

    # 板球胜负盘是 2-way MONEYLINE，锁定模型是追分（chase）局的 target/required，
    # 与比分差/剩余时间无关——必须在通用 _evaluate_moneyline 之前专属路由。
    if is_cricket_game(game):
        if market.market_type == SportsMarketType.MONEYLINE:
            return _evaluate_cricket_moneyline(candidate, policy)
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)

    # 手球胜负盘是 2-way MONEYLINE，但 livescore 无盘中时钟——锁定模型只依赖
    # 超大领先/已判定，与通用 lead+seconds 模型不同，专属路由。
    if is_handball_game(game):
        if market.market_type == SportsMarketType.MONEYLINE:
            return _evaluate_handball_moneyline(candidate, policy)
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)

    # 拳击 / MMA：盘中无可靠比分模型，且不建立回合评分模型（CLAUDE.md §17）。
    # 比赛结束后由 ENDED 路径的 ended-moneyline 用 winner 锁定胜方；进行中的
    # 任何盘口只给精确可审计拒绝原因，不退化到泛化的 missing_seconds_remaining。
    if is_combat_sport_game(game):
        return _reject(candidate, TailRejectReason.MMA_IN_PROGRESS_NO_MODEL.value)

    if is_tennis_game(game):
        if market.market_type == SportsMarketType.TOTALS:
            return _evaluate_tennis_totals(candidate, policy)
        if market.market_type == SportsMarketType.MONEYLINE:
            return _evaluate_tennis_moneyline(candidate, policy)
        if market.market_type == SportsMarketType.SPREADS:
            # 网球盘分让分（set handicap）可从盘数差锁定；网球局数让分目前
            # 仍无干净模型——只有盘分让分进专属评估器。
            if _is_tennis_set_handicap_market(market):
                return _evaluate_tennis_set_handicap(candidate, policy)
            return _reject(candidate, TailRejectReason.TENNIS_SPREADS_NOT_SUPPORTED.value)
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)

    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_totals(candidate, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _evaluate_moneyline(candidate, _sport_moneyline_policy(game, policy))
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_spreads(candidate, policy)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def evaluate_scale_in_opportunity(
    game: LiveGameState | None,
    market: SportsMarketSnapshot,
    *,
    policy: TailPolicy,
    now: datetime | None = None,
) -> TailEvaluation:
    """评估已有持仓是否达到受控加仓所需的更严格优势状态。"""

    family_reject_reason = _market_family_reject_reason(market.market_family)
    if family_reject_reason is not None:
        return _reject(
            None,
            family_reject_reason.value,
            metadata={
                "market_family": market.market_family.value,
                "market_type": market.market_type.value,
                "side": market.side.value,
                "line": str(market.line) if market.line is not None else None,
                "best_ask": str(market.best_ask) if market.best_ask is not None else None,
            },
        )
    if game is None:
        return _reject(None, TailRejectReason.MISSING_LIVE_GAME_STATE.value)

    candidate = _candidate(game, market)
    scope_reject_reason = _market_scope_reject_reason(market)
    if scope_reject_reason is not None:
        return _reject(candidate, scope_reject_reason.value)
    if game.status == LiveGameStatus.ENDED:
        market_reject_reason = _market_data_reject_reason(game, market, policy)
        if market_reject_reason:
            return _reject(candidate, market_reject_reason.value)
        ended = _evaluate_ended_not_closed(candidate, policy)
        if not ended.accepted:
            return ended
        return _accept(
            candidate,
            f"scale_in_{ended.reason.removeprefix('ended_not_closed_')}",
            ended.execution_permission or ExecutionPermission.AUTO_EXECUTE,
            opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
        )

    common_reject_reason = _common_reject_reason(game, market, policy, now=now)
    if common_reject_reason:
        return _reject(candidate, common_reject_reason.value)

    if is_tennis_game(game):
        scale_eval = _evaluate_tennis_scale_in(candidate, policy)
    elif is_esports_game(game):
        # esports 胜负盘只在系列赛锁定后入场，无渐进加仓窗口——不支持 scale-in。
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)
    elif market.market_type == SportsMarketType.TOTALS:
        scale_eval = _evaluate_totals_scale_in(candidate, policy)
    elif market.market_type == SportsMarketType.MONEYLINE:
        scale_eval = _evaluate_moneyline_scale_in(candidate, _sport_moneyline_policy(game, policy))
    elif market.market_type == SportsMarketType.SPREADS:
        scale_eval = _evaluate_spreads_scale_in(candidate, policy)
    else:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)
    # scale-in ACCEPT 前同样跑 math_lock veto——加仓不能加在已输方向。
    if scale_eval.accepted:
        veto_reason = _entry_math_lock_veto(candidate)
        if veto_reason is not None:
            return _reject(candidate, veto_reason)
    return scale_eval


# ---- 内部工具 -------------------------------------------------------


def _evaluate_ended_not_closed(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """用最终比分判断已结束但未封盘 market 的确定性方向。"""

    market = candidate.market
    # 运动专属 prop 家族即使比赛已结束也不可锁定（缺胜利方式/逐球员/F1 遥测
    # 数据）——ENDED 路径同样先给精确可审计原因，不退化到泛化兜底。
    sport_prop_reject = sport_specific_prop_reject_reason(market)
    if sport_prop_reject is not None:
        return _reject(candidate, sport_prop_reject.value)
    # ENDED 路径同样优先路由 anytime-goalscorer：终场后无任何匹配进球事件
    # 即可干净锁定 NO 方向（CLAUDE.md §17：可锁定机会不能被兜底原因吞掉）。
    if is_anytime_goalscorer_market(market):
        return _evaluate_anytime_goalscorer(candidate, policy)
    if is_tennis_game(candidate.game):
        return _evaluate_ended_tennis(candidate, policy)
    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_ended_totals(candidate, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _evaluate_ended_moneyline(candidate, policy)
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_ended_spreads(candidate, policy)
    # 利基事件型 prop 多在比赛结束才锁定（零封 YES / 平局退款 / 双重机会）——
    # ENDED 路径同样要分派到事件 prop 评估器，否则会被泛化原因误拒。
    if market.market_type == SportsMarketType.BINARY_PROP:
        precise_reject = event_prop_reject_reason(market)
        if precise_reject is not None:
            return _reject(candidate, precise_reject.value)
        if is_modeled_event_prop_market(market):
            return evaluate_event_prop(candidate, policy)
        # 终局态的 soccer/rugby 专属 binary_prop（精确比分/BTTS/半场赛果/
        # 3-way 胜负/橄榄球胜负）也要分派——最终比分已定,本是最干净的锁定。
        # 此前 ENDED 路径缺失这组分派,64+ 个 exact-score 终局市场落到兜底
        # UNSUPPORTED_MARKET_TYPE,与 §17"每个被拒市场要能回答为什么"相符
        # 但实际是可锁定机会被错失。
        if is_soccer_exact_score_market(market):
            return _evaluate_soccer_exact_score(candidate, policy)
        if is_soccer_btts_market(market):
            return _evaluate_soccer_btts(candidate, policy)
        if is_soccer_halftime_market(market):
            return _evaluate_soccer_halftime_result(candidate, policy)
        if is_soccer_game(candidate.game) and is_soccer_moneyline_market(market):
            return _evaluate_soccer_moneyline(candidate, policy)
        if is_rugby_game(candidate.game):
            return _evaluate_rugby_moneyline(candidate, policy)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def _evaluate_nfl_manual_review(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    if candidate.market.market_type == SportsMarketType.TOTALS:
        evaluation = _evaluate_totals(candidate, policy)
    elif candidate.market.market_type == SportsMarketType.MONEYLINE:
        evaluation = _evaluate_moneyline(candidate, policy)
    elif candidate.market.market_type == SportsMarketType.SPREADS:
        evaluation = _evaluate_spreads(candidate, policy)
    else:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)
    if not evaluation.accepted:
        return evaluation
    return _accept(candidate, "nfl_requires_manual_review", ExecutionPermission.MANUAL_CONFIRM)


def _has_score_conflict(game: LiveGameState) -> bool:
    """只有分数字段冲突才真正影响决策；status/period 过渡冲突（如 live↔unknown）忽略。"""
    _SCORE_FIELDS = frozenset({"home_score", "away_score", "home_goals", "away_goals", "score"})
    return any(c.get("field") in _SCORE_FIELDS for c in game.source_conflicts)


def _common_reject_reason(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
    *,
    now: datetime | None,
) -> TailRejectReason | None:
    if game.status != LiveGameStatus.LIVE:
        return TailRejectReason.GAME_NOT_LIVE
    if _has_score_conflict(game):
        return TailRejectReason.LIVE_SOURCE_CONFLICT
    if _is_stale(game, policy, now=now):
        return TailRejectReason.STALE_GAME_STATE
    # BINARY_PROP 无传统盘口价格结构，跳过 ask/流动性检查；
    # 后续由体育专属评估器（目前是 binary_prop_no_tail_model）统一处理。
    if market.market_type == SportsMarketType.BINARY_PROP:
        return None
    if market.best_ask is None:
        return TailRejectReason.MISSING_BEST_ASK
    # price/liquidity 入场 gate 已删——宽进严管，持仓策略接管止盈止损。
    return None


def _market_data_reject_reason(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> TailRejectReason | None:
    """检查不依赖比赛是否 live 的盘口和来源门槛。"""

    if _has_score_conflict(game):
        return TailRejectReason.LIVE_SOURCE_CONFLICT
    # BINARY_PROP 无传统盘口价格结构（_max_entry_price 对其返回 0），与
    # _common_reject_reason 保持一致跳过 ask/流动性检查；价格门禁由事件
    # prop 评估器用 _binary_prop_price_reject 自查。
    if market.market_type == SportsMarketType.BINARY_PROP:
        return None
    if market.best_ask is None:
        return TailRejectReason.MISSING_BEST_ASK
    # price/liquidity 入场 gate 已删——同上。
    return None


def _max_entry_price(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> Decimal:
    if (
        is_tennis_game(game)
        and _is_tennis_set_winner_market(market)
        and game.tennis_state is not None
        and _tennis_set_winner_completed_for_side(game.tennis_state, market)
    ):
        return policy.locked_outcome_max_entry_price
    if market.market_type == SportsMarketType.TOTALS:
        return policy.totals_max_entry_price
    if market.market_type == SportsMarketType.MONEYLINE:
        return policy.moneyline_max_entry_price
    if market.market_type == SportsMarketType.SPREADS:
        return policy.spreads_max_entry_price
    return Decimal("0")


def _is_stale(
    game: LiveGameState,
    policy: TailPolicy,
    *,
    now: datetime | None,
) -> bool:
    if game.observed_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    observed_at = game.observed_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age_seconds = (current_time - observed_at).total_seconds()
    if is_tennis_game(game):
        max_age_seconds = policy.tennis_max_game_state_age_seconds
    elif is_mlb_game(game) and game.baseball_state is not None:
        max_age_seconds = policy.baseball_max_game_state_age_seconds
    elif is_esports_game(game):
        max_age_seconds = policy.esports_max_game_state_age_seconds
    elif is_soccer_game(game):
        max_age_seconds = policy.soccer_max_game_state_age_seconds
    else:
        max_age_seconds = policy.max_game_state_age_seconds
    return age_seconds > max_age_seconds



def _sport_moneyline_policy(game: LiveGameState, policy: TailPolicy) -> TailPolicy:
    """为低分运动覆盖 min_moneyline_lead，避免足球/冰球被 6 分 lead 要求完全封住。"""

    if is_soccer_game(game):
        return dataclasses.replace(policy, min_moneyline_lead=policy.soccer_min_moneyline_lead)
    if is_hockey_game(game):
        return dataclasses.replace(policy, min_moneyline_lead=policy.hockey_min_moneyline_lead)
    return policy
