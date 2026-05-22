"""运动专属 prop 家族的精确识别与可审计拒绝原因。

以下 prop 家族此前全部落到评估器的泛化 ``binary_prop`` 分支，被泛化原因
（``binary_prop_no_tail_model`` / ``OUTCOME_NOT_LOCKED``）拒绝。CLAUDE.md §17
要求每个被拒市场都能回答"为什么不做这个机会"——对一个清晰可识别的 prop
家族返回泛化原因违反 §17，且会误导排查（看起来像解析失败，而非"该家族
被刻意未建模"）。本模块为以下三类各加精确识别与 distinct 拒绝原因：

  - 拳击/MMA 胜利方式（method-of-victory）—— KO/TKO、降服、判定、"打满
    全程"、回合组。只有比赛以特定方式结束时才结算，盘中永不可扫尾锁定。
  - F1 子盘口（F1 props）—— 杆位、登台、最快圈速、安全车等。需要 F1 专属
    遥测，本系统未建模。整场冠军（race winner）不在此列。
  - 板球 prop（cricket props）—— 掷币胜方、最佳击球手、最多六分球等。
    需要逐球员板球数据，本系统未接入。

识别优先用 Polymarket Gamma 的 ``sportsMarketType`` 字段——它对这三类家族
可靠填充（不同于多数常规盘口该字段为空）；缺失时回退 slug/question 关键字。
三类都不强行建模——正确的 §17 处理是精确可审计拒绝原因，而非半成品模型。
"""

from __future__ import annotations

from strategies.sports_framework import SportsMarketSnapshot

from .types import TailRejectReason


# ---- sportsMarketType 前缀（首选识别信号） ----------------------------

# 拳击/MMA 胜利方式：Gamma sportsMarketType 形如 ``ufc_method_of_victory``。
_METHOD_OF_VICTORY_TYPES = frozenset({"ufc_method_of_victory"})
# F1 整场冠军——不归 F1-prop 拒绝（由 RACE-kind 直播匹配链路另行处理）。
_F1_RACE_WINNER_TYPES = frozenset({"f1_race_winner"})
# F1 子盘口：除 race winner 外的所有 ``f1_*`` sportsMarketType 都归此类。


def _normalized_type(market: SportsMarketSnapshot) -> str:
    return (market.sports_market_type or "").strip().lower()


def _slug(market: SportsMarketSnapshot) -> str:
    return (market.market_slug or "").strip().lower()


# ---- 拳击/MMA 胜利方式 -----------------------------------------------

# slug/question 回退关键字：sportsMarketType 缺失时使用。
_METHOD_OF_VICTORY_SLUG_KEYWORDS = (
    "method-of-victory",
    "win-by-ko",
    "win-by-tko",
    "by-ko-tko",
    "ko-tko",
    "win-by-submission",
    "by-submission",
    "win-by-decision",
    "by-decision",
    "go-the-distance",
    "fight-to-go-the-distance",
    "round-group",
    "winning-round",
    "round-of-victory",
)


def is_method_of_victory_market(market: SportsMarketSnapshot) -> bool:
    """识别拳击/MMA 胜利方式盘口。

    首选 sportsMarketType（``ufc_method_of_victory`` 等），缺失时回退 slug
    关键字（win-by-ko / submission / decision / go-the-distance / round-group）。
    """
    if _normalized_type(market) in _METHOD_OF_VICTORY_TYPES:
        return True
    slug = _slug(market)
    return any(keyword in slug for keyword in _METHOD_OF_VICTORY_SLUG_KEYWORDS)


# ---- F1 子盘口 -------------------------------------------------------

# slug/question 回退关键字：sportsMarketType 缺失时使用。
_F1_PROP_SLUG_KEYWORDS = (
    "qualifying-pole",
    "pole-position",
    "race-podium",
    "podium-finish",
    "fastest-lap",
    "safety-car",
    "first-retirement",
    "first-dnf",
    "winning-constructor",
    "constructors-points",
    "driver-points-finish",
)
# F1 整场冠军 slug 回退关键字——命中即视为 race winner，不归 F1-prop 拒绝。
_F1_RACE_WINNER_SLUG_KEYWORDS = (
    "f1-race-winner",
    "grand-prix-winner",
    "race-winner",
)


def is_f1_race_winner_market(market: SportsMarketSnapshot) -> bool:
    """识别 F1 整场冠军盘口（race winner）。

    race winner 由 RACE-kind 直播匹配链路另行处理，不归 F1-prop 精确拒绝；
    本函数用于把它从 F1 子盘口识别中排除。
    """
    if _normalized_type(market) in _F1_RACE_WINNER_TYPES:
        return True
    # sportsMarketType 已是其它 f1_* 时不能再凭 slug 误判为 race winner。
    if _normalized_type(market).startswith("f1_"):
        return False
    slug = _slug(market)
    return any(keyword in slug for keyword in _F1_RACE_WINNER_SLUG_KEYWORDS)


def is_f1_prop_market(market: SportsMarketSnapshot) -> bool:
    """识别 F1 子盘口（杆位/登台/最快圈速/安全车等）。

    首选 sportsMarketType：任意 ``f1_*`` 但非 ``f1_race_winner`` 即归此类；
    缺失时回退 slug 关键字。整场冠军（race winner）显式排除。
    """
    market_type = _normalized_type(market)
    if market_type.startswith("f1_"):
        return market_type not in _F1_RACE_WINNER_TYPES
    if is_f1_race_winner_market(market):
        return False
    slug = _slug(market)
    return any(keyword in slug for keyword in _F1_PROP_SLUG_KEYWORDS)


# ---- 板球 prop -------------------------------------------------------

# slug/question 回退关键字：sportsMarketType 缺失时使用。
_CRICKET_PROP_SLUG_KEYWORDS = (
    "toss-winner",
    "win-the-toss",
    "top-batter",
    "top-batsman",
    "top-bowler",
    "most-sixes",
    "most-fours",
    "most-runs",
    "most-wickets",
    "highest-opening-partnership",
    "man-of-the-match",
    "player-of-the-match",
    "method-of-dismissal",
    "highest-individual-score",
)


def is_cricket_prop_market(market: SportsMarketSnapshot) -> bool:
    """识别板球 prop 盘口（掷币胜方/最佳击球手/最多六分球等）。

    首选 sportsMarketType：任意 ``cricket_*`` 即归此类；缺失时回退 slug
    关键字。板球整场胜负盘走 single_game 胜负评估器，不在此列——其
    sportsMarketType 不以 ``cricket_`` 前缀标记 prop。
    """
    if _normalized_type(market).startswith("cricket_"):
        return True
    slug = _slug(market)
    return any(keyword in slug for keyword in _CRICKET_PROP_SLUG_KEYWORDS)


# ---- 统一分派入口 ----------------------------------------------------


def sport_specific_prop_reject_reason(
    market: SportsMarketSnapshot,
) -> TailRejectReason | None:
    """若 market 属于已知的运动专属 prop 家族，返回其 distinct 拒绝原因；否则 None。

    这三类 prop 家族都不可扫尾锁定，也没有所需的专属数据源/定价模型——统一
    在这里给精确可审计原因，绝不退化到泛化的 ``binary_prop_no_tail_model`` /
    ``OUTCOME_NOT_LOCKED``。F1 整场冠军（race winner）显式不在此列。
    """
    if is_method_of_victory_market(market):
        return TailRejectReason.UNSUPPORTED_METHOD_OF_VICTORY
    if is_f1_prop_market(market):
        return TailRejectReason.UNSUPPORTED_F1_PROP
    if is_cricket_prop_market(market):
        return TailRejectReason.UNSUPPORTED_CRICKET_PROP
    return None
