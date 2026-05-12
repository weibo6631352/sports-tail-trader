"""Series 评估类型层。

与 ``outright/types.py`` 平行：series family 涵盖 NBA/NHL/MLB 季后赛系列赛及类似
盘口。本模块只承载类型骨架，定价模型在 Worktree 3-4 接入。

设计原则与 outright 一致：所有拒绝原因可审计，accepted/rejected 用同一份
``SeriesEvaluation`` dataclass 表达，便于 ``strategy.decide_entry`` 投影成
``ExtensionDecision``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.domain.market import Market


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
    # 第三方系列赛赔率缺失——反向定价无锚点。
    MISSING_SERIES_ODDS = "missing_series_odds"
    STALE_SERIES_ODDS = "stale_series_odds"
    INSUFFICIENT_EDGE = "insufficient_edge"
    PRICE_ABOVE_FAIR = "price_above_fair"
    LIQUIDITY_BELOW_MIN = "liquidity_below_min"
    MARKET_END_PASSED = "market_end_passed"
    HOLD_HORIZON_EXCEEDED = "hold_horizon_exceeded"
    OUTCOME_RESOLVED = "outcome_resolved"
    SOURCE_CONFLICT = "source_conflict"
    # 各子类型模型接线尚未落地（Worktree 3-4 替换为真实拒绝/接受路径）。
    WINNER_MODEL_PENDING = "winner_model_pending"
    TOTAL_GAMES_MODEL_PENDING = "total_games_model_pending"
    HANDICAP_MODEL_PENDING = "handicap_model_pending"


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
class SeriesState:
    """系列赛热态快照。

    本 worktree 仅承载类型定义；数据采集与匹配在 Worktree 3 落地。所有定价模型
    都从这里读热态，不允许从原始 ``ExtensionContext.metadata`` 读裸字段。
    """

    team_a: str
    team_b: str
    wins_a: int
    wins_b: int
    best_of: int
    next_game_at: datetime | None
    observed_at: datetime


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
