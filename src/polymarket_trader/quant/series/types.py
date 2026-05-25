"""Series 评估类型层。

与 ``outright/types.py`` 平行：series family 涵盖 NBA/NHL/MLB 季后赛系列赛及类似
盘口。本模块只承载类型骨架，定价模型在 Worktree 3-4 接入。

设计原则与 outright 一致：所有拒绝原因可审计，accepted/rejected 用同一份
``SeriesEvaluation`` dataclass 表达，便于 ``strategy.decide_entry`` 投影成
``TradingDecision``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.contracts.live_state import SeriesState as SeriesState  # re-export


class SeriesSubType(StrEnum):
    """系列赛盘口子类型。

    分类驱动定价模型选择：WINNER 走系列赛胜者概率，TOTAL_GAMES 走系列赛局数
    分布，GAME_HANDICAP 走单场让分（在系列赛上下文里）。
    """

    WINNER = "winner"
    TOTAL_GAMES = "total_games"
    GAME_HANDICAP = "game_handicap"
    OTHER = "other"


class SeriesRejectReason(StrEnum):
    """series 评估拒绝原因。"""

    # 文本无法分类到 winner/total_games/game_handicap 子类型——若上游已经把市场归到
    # SERIES family，说明 classifier 有覆盖盲区，应进 audit 调查。
    SUBTYPE_UNCLASSIFIED = "subtype_unclassified"
    # 系列赛热态（比分、剩余场次等）缺失——模型无法给出 fair_value。
    MISSING_SERIES_STATE = "missing_series_state"
    STALE_SERIES_STATE = "stale_series_state"
    # 单场胜率 (p_per_game) 缺失——无法把系列赛胜率算出来。
    MISSING_SERIES_ODDS = "missing_series_odds"
    STALE_SERIES_ODDS = "stale_series_odds"
    # 系列赛 outcome 文本既不是球队名也无法从市场文本反推到 team_a/team_b。
    SERIES_TEAM_NOT_RESOLVED = "series_team_not_resolved"
    MISSING_BEST_ASK = "missing_best_ask"
    INSUFFICIENT_EDGE = "insufficient_edge"
    PRICE_ABOVE_FAIR = "price_above_fair"
    LIQUIDITY_BELOW_MIN = "liquidity_below_min"
    MARKET_END_PASSED = "market_end_passed"
    HOLD_HORIZON_EXCEEDED = "hold_horizon_exceeded"
    MIN_REMAINING_DAYS_NOT_MET = "min_remaining_days_not_met"
    OUTCOME_RESOLVED = "outcome_resolved"
    SOURCE_CONFLICT = "source_conflict"
    # 预算 / 相关性硬上限（与 outright 同语义）。
    TOTAL_BUDGET_EXHAUSTED = "total_budget_exhausted"
    EVENT_CORRELATION_CAP = "event_correlation_cap"
    PER_MARKET_CAP_EXCEEDED = "per_market_cap_exceeded"
    # outcome 文本无法解析为 (line, direction) (TOTAL_GAMES) 或 (team, line, scope)
    # (GAME_HANDICAP) —— evaluator 已分到正确子类型但 outcome 形态超出已知模式。
    SERIES_OUTCOME_NOT_PARSED = "series_outcome_not_parsed"
    # GAME_HANDICAP 子类型且 scope=single_game，但 metadata 缺少 game_spreads
    # （TheOddsAPI spread fetch 失败或未匹配上）。series scope 的 handicap 仍可
    # 走 series_handicap_cover_probability。
    MISSING_GAME_SPREADS = "missing_game_spreads"
    STALE_GAME_SPREADS = "stale_game_spreads"


@dataclass(frozen=True, slots=True)
class SeriesCandidate:
    """series 评估候选。"""

    market: Market
    outcome_label: str
    token_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def market_slug(self) -> str:
        return self.market.market_slug

    @property
    def condition_id(self) -> str:
        return self.market.condition_id



@dataclass(frozen=True, slots=True)
class SeriesEvaluation:
    """series 评估结果。accepted=True 时填 fair_value/metadata；False 时填 reject_reason。"""

    accepted: bool
    sub_type: SeriesSubType
    candidate: SeriesCandidate
    fair_value: Decimal | None = None
    reject_reason: SeriesRejectReason | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


__all__ = [
    "SeriesCandidate",
    "SeriesEvaluation",
    "SeriesRejectReason",
    "SeriesState",
    "SeriesSubType",
]
