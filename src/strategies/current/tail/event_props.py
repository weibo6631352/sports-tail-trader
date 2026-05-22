"""利基事件型 prop 盘口的识别与扫尾评估。

这些盘口此前全部落到通用 ``binary_prop`` 分支，被泛化原因
（``OUTCOME_NOT_LOCKED`` / ``binary_prop_no_tail_model``）拒绝。CLAUDE.md §17
要求每个被拒市场都能回答"为什么不做这个机会"——泛化原因对一个具体的、
未建模的 prop 类型不可审计。本模块为以下 7 类各加精确识别与原因：

  - odd/even total（总分奇偶）—— 精确拒绝：每进一分奇偶翻转，永不可扫尾锁定。
  - winning-margin（精确净胜分桶）—— 精确拒绝：终场前净胜分仍可变，不可锁定。
  - to-score-first / first-goal（首个得分方）—— 精确拒绝：归一化直播模型
    （LiveEvent / *GameState）不携带首得分方或进球时间线，缺数据不臆测。
  - clean-sheet（零封）—— 建模：对手一旦进球即 NO 锁定；终场对手 0 分即 YES 锁定。
  - race-to-N（先到 N 分/球）—— 建模：某方比分先到 N 且对手未到 → 该方锁定。
  - draw-no-bet（平局退款）—— 建模：2-way 胜负盘变体，复用净胜分锁定逻辑。
  - double-chance（双重机会，X 胜或平）—— 建模：对手再不能赢 → YES 锁定；
    对手已赢 → NO 锁定。

精确拒绝类只给 distinct 的 ``TailRejectReason``；建模类走真实评估器，做
``Decimal`` 比分数学并返回可审计 accept/reject。

识别信号：odd/even total 与 to-score-first 首选 Gamma ``sportsMarketType``
（``basketball_odd_even`` / ``basketball_team_to_score_first``），缺失时回退
slug 关键字。winning-margin / clean-sheet / race-to-N / draw-no-bet /
double-chance 在真实 Gamma 数据中无对应 ``sportsMarketType``，只能用 slug。
"""

from __future__ import annotations

import re

from strategies.sports_framework import (
    LiveGameStatus,
    SportsMarketSide,
    SportsMarketSnapshot,
)

from .types import (
    SportsTailCandidate,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)
from .core import _accept, _reject


# ---- slug 工具 --------------------------------------------------------


def _slug(market: SportsMarketSnapshot) -> str:
    return (market.market_slug or "").strip().lower()


def _slug_parts(market: SportsMarketSnapshot) -> list[str]:
    return [p for p in _slug(market).split("-") if p]


def _normalized_type(market: SportsMarketSnapshot) -> str:
    return (market.sports_market_type or "").strip().lower()


# ---- sportsMarketType 前缀（首选识别信号） ----------------------------
#
# Polymarket Gamma 的 ``sportsMarketType`` 对部分利基 prop 家族会可靠填充，
# 是优于 slug-后缀猜测的类型信号；下列字符串均取自真实 Gamma 数据。其余
# 家族（winning-margin / clean-sheet / race-to-N / draw-no-bet / double-chance）
# 当前在真实数据中无对应 ``sportsMarketType``，仍只能依赖 slug 关键字回退。

# 总分奇偶：常规运动以 ``{sport}_odd_even`` 标记（如 ``basketball_odd_even``）。
# 电竞的 ``cs2_odd_even_total_*`` / ``lol_odd_even_total_kills`` 走 esports 评估器，
# 不进本模块，因此这里只收常规运动的 odd/even 类型。
_ODD_EVEN_TYPES = frozenset({"basketball_odd_even"})

# 首得分方：常规运动以 ``{sport}_team_to_score_first`` 标记。
_TO_SCORE_FIRST_TYPES = frozenset({"basketball_team_to_score_first"})


# ---- odd/even total（精确拒绝） ---------------------------------------


def is_odd_even_total_market(market: SportsMarketSnapshot) -> bool:
    """识别总分奇偶盘口（total points/goals odd or even）。

    首选 Gamma ``sportsMarketType``（``basketball_odd_even`` 等）——slug 拼写
    与预期不符时仍能识别；该字段为空时回退 slug 关键字。
    """
    if _normalized_type(market) in _ODD_EVEN_TYPES:
        return True
    slug = _slug(market)
    # "odd-even" / "total-odd" / "total-even" / "odd-or-even" 等常见拼法。
    return (
        "odd-even" in slug
        or "even-odd" in slug
        or slug.endswith("-odd")
        or slug.endswith("-even")
        or "total-odd" in slug
        or "total-even" in slug
        or "odd-or-even" in slug
    )


# ---- winning-margin（精确拒绝） --------------------------------------


def is_winning_margin_market(market: SportsMarketSnapshot) -> bool:
    """识别精确净胜分桶盘口（exact winning margin bucket）。"""
    slug = _slug(market)
    return (
        "winning-margin" in slug
        or "win-margin" in slug
        or "victory-margin" in slug
        or "margin-of-victory" in slug
    )


# ---- to-score-first / first-goal（精确拒绝） -------------------------


def is_to_score_first_market(market: SportsMarketSnapshot) -> bool:
    """识别首个得分方盘口（to score first / first goal / first to score）。

    首选 Gamma ``sportsMarketType``（``basketball_team_to_score_first`` 等）——
    slug 拼写与预期不符时仍能识别；该字段为空时回退 slug 关键字。
    """
    if _normalized_type(market) in _TO_SCORE_FIRST_TYPES:
        return True
    slug = _slug(market)
    return (
        "score-first" in slug
        or "first-goal" in slug
        or "first-to-score" in slug
        or "first-scorer" in slug
        or "first-team-to-score" in slug
        or "opening-goal" in slug
    )


# ---- clean-sheet（建模） ---------------------------------------------


def _clean_sheet_team_side(market: SportsMarketSnapshot) -> str | None:
    """从 slug 解析零封盘口针对的球队方（home / away）。

    slug 形如 ``...-clean-sheet-{home|away|abbr}``：末段为 home/away 直接返回；
    末段为球队缩写时与 7 段足球 slug 的主/客缩写比对。识别不出返回 None。
    """
    slug = _slug(market)
    if "clean-sheet" not in slug and "cleansheet" not in slug:
        return None
    parts = _slug_parts(market)
    if not parts:
        return None
    suffix = parts[-1]
    if suffix in {"home", "away"}:
        return suffix
    # 7 段足球 slug：league-home-away-yyyy-mm-dd-...；末段是球队缩写时比对。
    if len(parts) >= 7:
        home_abbr, away_abbr = parts[1], parts[2]
        if suffix == home_abbr:
            return "home"
        if suffix == away_abbr:
            return "away"
    return None


def is_clean_sheet_market(market: SportsMarketSnapshot) -> bool:
    """识别零封盘口（某队是否一球不丢）。"""
    return _clean_sheet_team_side(market) is not None


def _evaluate_clean_sheet(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """零封盘口评估：某队整场是否一球不丢（对手得分为 0）。

    比分只增不减——对手一旦进球，零封永不可能成立 → 盘中即 NO 锁定。
    YES 只有在比赛 ENDED 且对手仍为 0 分时才确定（盘中对手随时可能进球）。
    binary_prop 在通用门禁跳过 ask 检查，这里自查入场价。
    """
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    team_side = _clean_sheet_team_side(market)
    if team_side is None:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SCOPE.value)
    price_reject = _binary_prop_price_reject(market, policy)
    if price_reject is not None:
        return _reject(candidate, price_reject.value)

    # 该队的"对手"得分：team_side=home 看 away_score，反之亦然。
    opponent_score = game.away_score if team_side == "home" else game.home_score
    opponent_scored = opponent_score >= 1
    game_ended = game.status == LiveGameStatus.ENDED

    if market.side == SportsMarketSide.NO:
        # 对手已进球 → 零封不可能 → NO 100% 锁定（盘中即可）。
        if opponent_scored:
            return _accept(
                candidate, "clean_sheet_no_locked", policy.totals_execution_permission
            )
        # 终场对手 0 分 → 零封成立 → NO 必败，不锁定。
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    # side YES：只有终场对手仍 0 分才锁定。
    if game_ended and not opponent_scored:
        return _accept(
            candidate, "clean_sheet_yes_locked", policy.totals_execution_permission
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# ---- race-to-N（建模） -----------------------------------------------


def _race_to_n(market: SportsMarketSnapshot) -> tuple[int, str | None] | None:
    """从 slug 解析 race-to-N 盘口的目标分 N 与针对的球队方。

    返回 ``(n, side)``：side 为 home/away（slug 点名了哪支队先到 N），
    或 None（未点名队、需由 outcome 方向判定，此时整盘按 YES/NO 不可解）。
    slug 形如 ``...-race-to-{n}-{home|away|abbr}`` 或 ``...-first-to-{n}-...``。
    """
    slug = _slug(market)
    match = re.search(r"(?:race-to|first-to)-(\d+)", slug)
    if match is None:
        return None
    try:
        n = int(match.group(1))
    except ValueError:
        return None
    if n <= 0:
        return None
    parts = _slug_parts(market)
    suffix = parts[-1] if parts else ""
    if suffix in {"home", "away"}:
        return n, suffix
    if len(parts) >= 7:
        home_abbr, away_abbr = parts[1], parts[2]
        if suffix == home_abbr:
            return n, "home"
        if suffix == away_abbr:
            return n, "away"
    return n, None


def is_race_to_n_market(market: SportsMarketSnapshot) -> bool:
    """识别 race-to-N 盘口（先到 N 分/球）。"""
    return _race_to_n(market) is not None


def _evaluate_race_to_n(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """race-to-N 盘口评估：哪支队先到 N 分/球。

    一旦某方比分达到 N 且对手仍 < N → 该方已是"先到 N"方，结果锁定
    （比分只增不减，对手追不回先后顺序）。需 slug 点名了针对的球队方
    （home/away）才能判定 YES/NO；未点名时给可审计 scope 拒绝。
    binary_prop 在通用门禁跳过 ask 检查，这里自查入场价。
    """
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    parsed = _race_to_n(market)
    if parsed is None:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SCOPE.value)
    n, team_side = parsed
    if team_side is None:
        # 未点名球队方——无法把 YES/NO 映射到具体一方，不臆测。
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SCOPE.value)
    price_reject = _binary_prop_price_reject(market, policy)
    if price_reject is not None:
        return _reject(candidate, price_reject.value)

    team_score = game.home_score if team_side == "home" else game.away_score
    opponent_score = game.away_score if team_side == "home" else game.home_score
    team_reached = team_score >= n and opponent_score < n
    opponent_reached = opponent_score >= n and team_score < n

    if market.side == SportsMarketSide.YES:
        # 点名队已先到 N → YES 锁定。
        if team_reached:
            return _accept(
                candidate, "race_to_n_yes_locked", policy.totals_execution_permission
            )
        # 对手已先到 N → 点名队不可能先到 → YES 必败，不锁定。
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    # side NO：对手已先到 N → 点名队赢不了 → NO 锁定。
    if opponent_reached:
        return _accept(
            candidate, "race_to_n_no_locked", policy.totals_execution_permission
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# ---- draw-no-bet（建模） ---------------------------------------------


def _draw_no_bet_side(market: SportsMarketSnapshot) -> str | None:
    """从 slug 解析平局退款盘口针对的球队方（home / away）。

    slug 形如 ``...-draw-no-bet-{home|away|abbr}`` / ``...-dnb-{...}``。
    """
    slug = _slug(market)
    if "draw-no-bet" not in slug and "-dnb" not in slug and "dnb-" not in slug:
        return None
    parts = _slug_parts(market)
    if not parts:
        return None
    suffix = parts[-1]
    if suffix in {"home", "away"}:
        return suffix
    if len(parts) >= 7:
        home_abbr, away_abbr = parts[1], parts[2]
        if suffix == home_abbr:
            return "home"
        if suffix == away_abbr:
            return "away"
    return None


def is_draw_no_bet_market(market: SportsMarketSnapshot) -> bool:
    """识别平局退款盘口（draw-no-bet）。"""
    return _draw_no_bet_side(market) is not None


def _evaluate_draw_no_bet(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """平局退款盘口评估。

    DNB 语义：押注某方获胜，平局则全额退款——结算上等价于 2-way 胜负盘
    （平局不影响下注方盈亏方向，只是退款）。因此扫尾锁定可直接复用净胜分
    逻辑：终场（ENDED）该方净胜分 > 0 → YES 锁定；净胜分 < 0（对手已赢）
    → YES 必败、NO 锁定。盘中不锁定——领先随时可能被追平/反超，平局退款
    使"领先即锁定"不成立（DNB 在平局是退款而非赢，需明确胜负）。
    binary_prop 在通用门禁跳过 ask 检查，这里自查入场价。
    """
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    team_side = _draw_no_bet_side(market)
    if team_side is None:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SCOPE.value)
    price_reject = _binary_prop_price_reject(market, policy)
    if price_reject is not None:
        return _reject(candidate, price_reject.value)

    # 平局退款使盘中"领先即锁"不成立——只在终场判定胜负。
    if game.status != LiveGameStatus.ENDED:
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    lead = (
        game.home_score - game.away_score
        if team_side == "home"
        else game.away_score - game.home_score
    )
    if market.side == SportsMarketSide.YES:
        if lead > 0:
            return _accept(
                candidate, "draw_no_bet_yes_locked", policy.moneyline_execution_permission
            )
        # 平局退款，YES 既未赢也未输（退款）——不构成 YES 锁定。
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)
    # side NO：对手已赢（点名方落后）→ NO 锁定。平局退款，NO 也不锁定。
    if lead < 0:
        return _accept(
            candidate, "draw_no_bet_no_locked", policy.moneyline_execution_permission
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# ---- double-chance（建模） -------------------------------------------


def _double_chance_covered_sides(market: SportsMarketSnapshot) -> frozenset[str] | None:
    """解析双重机会盘口覆盖的两个结果。

    返回结果集合的子集，元素取自 ``{"home", "away", "draw"}``：
      - "1X" / home-draw → {home, draw}
      - "X2" / draw-away → {draw, away}
      - "12" / home-away → {home, away}
    slug 形如 ``...-double-chance-{1x|x2|12}`` 或 ``...-double-chance-{home|away}``
    （末段单球队方时表示"该队或平"）。识别不出返回 None。
    """
    slug = _slug(market)
    if "double-chance" not in slug:
        return None
    parts = _slug_parts(market)
    if not parts:
        return None
    suffix = parts[-1]
    code_map = {
        "1x": frozenset({"home", "draw"}),
        "x1": frozenset({"home", "draw"}),
        "x2": frozenset({"draw", "away"}),
        "2x": frozenset({"draw", "away"}),
        "12": frozenset({"home", "away"}),
        "21": frozenset({"home", "away"}),
    }
    if suffix in code_map:
        return code_map[suffix]
    # 末段是单球队方："{team} or draw" 语义。
    if suffix == "home":
        return frozenset({"home", "draw"})
    if suffix == "away":
        return frozenset({"draw", "away"})
    if len(parts) >= 7:
        home_abbr, away_abbr = parts[1], parts[2]
        if suffix == home_abbr:
            return frozenset({"home", "draw"})
        if suffix == away_abbr:
            return frozenset({"draw", "away"})
    return None


def is_double_chance_market(market: SportsMarketSnapshot) -> bool:
    """识别双重机会盘口（double-chance）。"""
    return _double_chance_covered_sides(market) is not None


def _evaluate_double_chance(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """双重机会盘口评估（覆盖 3-way 结果中的两个）。

    "X 或平"（{home,draw} 或 {draw,away}）锁定逻辑：
      - YES 锁定：被排除的那个结果（唯一未被覆盖的一方获胜）已不可能
        发生。即终场（ENDED）实际结果落在覆盖集合内。
      - NO 锁定：被排除方已确定获胜——终场实际结果不在覆盖集合内。
    "12"（{home,away}，即不平）盘中可在终场判定。三类都在终场判定即可
    干净锁定；盘中比分可变不锁定。binary_prop 在通用门禁跳过 ask 检查。
    """
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    covered = _double_chance_covered_sides(market)
    if covered is None:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SCOPE.value)
    price_reject = _binary_prop_price_reject(market, policy)
    if price_reject is not None:
        return _reject(candidate, price_reject.value)

    # 盘中比分仍可变——只在终场判定实际结果。
    if game.status != LiveGameStatus.ENDED:
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    if game.home_score > game.away_score:
        actual = "home"
    elif game.away_score > game.home_score:
        actual = "away"
    else:
        actual = "draw"
    outcome_covered = actual in covered

    if market.side == SportsMarketSide.YES:
        if outcome_covered:
            return _accept(
                candidate, "double_chance_yes_locked", policy.moneyline_execution_permission
            )
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)
    # side NO：实际结果不在覆盖集合 → NO 锁定。
    if not outcome_covered:
        return _accept(
            candidate, "double_chance_no_locked", policy.moneyline_execution_permission
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# ---- 共享门禁 --------------------------------------------------------


def _binary_prop_price_reject(
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> TailRejectReason | None:
    """binary_prop 盘口的入场价/流动性自查（通用门禁对 binary_prop 跳过了这些）。"""
    if market.best_ask is None:
        return TailRejectReason.MISSING_BEST_ASK
    if market.best_ask < policy.min_entry_price:
        return TailRejectReason.PRICE_BELOW_MIN
    if market.best_ask > policy.totals_max_entry_price:
        return TailRejectReason.PRICE_ABOVE_MAX
    if market.buyable_liquidity_usdc < policy.min_liquidity_usdc:
        return TailRejectReason.LIQUIDITY_BELOW_MIN
    return None


# ---- 统一分派入口 ----------------------------------------------------


def event_prop_reject_reason(market: SportsMarketSnapshot) -> TailRejectReason | None:
    """若 market 是精确拒绝类利基 prop，返回其 distinct 拒绝原因；否则 None。

    精确拒绝类（odd/even、winning-margin、to-score-first）没有干净的扫尾
    锁定模型或缺必要直播数据，统一在这里给可审计原因，绝不退化到泛化的
    ``OUTCOME_NOT_LOCKED`` / ``binary_prop_no_tail_model``。
    """
    if is_odd_even_total_market(market):
        return TailRejectReason.UNSUPPORTED_ODD_EVEN
    if is_winning_margin_market(market):
        return TailRejectReason.UNSUPPORTED_WINNING_MARGIN
    if is_to_score_first_market(market):
        return TailRejectReason.UNSUPPORTED_TO_SCORE_FIRST
    return None


def is_modeled_event_prop_market(market: SportsMarketSnapshot) -> bool:
    """该 market 是否属于本模块已建模（可锁定）的利基 prop。"""
    return (
        is_clean_sheet_market(market)
        or is_race_to_n_market(market)
        or is_draw_no_bet_market(market)
        or is_double_chance_market(market)
    )


def evaluate_event_prop(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """分派到已建模利基 prop 的评估器。

    调用前应先确认 ``is_modeled_event_prop_market`` 为真；分派顺序按 slug
    标记唯一性排列，互不重叠。
    """
    market = candidate.market
    if is_clean_sheet_market(market):
        return _evaluate_clean_sheet(candidate, policy)
    if is_race_to_n_market(market):
        return _evaluate_race_to_n(candidate, policy)
    if is_draw_no_bet_market(market):
        return _evaluate_draw_no_bet(candidate, policy)
    if is_double_chance_market(market):
        return _evaluate_double_chance(candidate, policy)
    return _reject(candidate, "binary_prop_no_tail_model")
