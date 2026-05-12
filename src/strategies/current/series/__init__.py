"""Series 市场子包：系列赛胜者 / 总局数 / 让分等系列赛盘口家族。

与 ``current/outright/`` ``current/tail/`` 平行：不同 family 各自负责子类型分类、
定价、风控和分配，由顶层 ``strategy.py`` 按 ``descriptor.market_family`` 分派。

Worktree 3 阶段接通 WINNER 子类型的完整链路（live series state + 单场胜率 +
负二项分布求和 + edge_gates）；TOTAL_GAMES / GAME_HANDICAP 仍在 Worktree 4 接入。
"""

from strategies.current.series.classifier import classify_series_sub_type
from strategies.current.series.evaluator import (
    SeriesEvaluatorInputs,
    evaluate_series_opportunity,
)
from strategies.current.series.match import (
    GameOdds,
    game_odds_from_metadata,
    series_state_from_metadata,
)
from strategies.current.series.risk import check_series_entry_risk
from strategies.current.series.single_game_prob import (
    SingleGameProb,
    derive_single_game_prob,
)
from strategies.current.series.team_resolver import TeamSide, resolve_series_team
from strategies.current.series.types import (
    SeriesCandidate,
    SeriesEvaluation,
    SeriesRejectReason,
    SeriesState,
    SeriesSubType,
)
from strategies.current.series.winner_model import (
    series_win_probability,
    team_b_win_probability,
)

__all__ = [
    "GameOdds",
    "SeriesCandidate",
    "SeriesEvaluation",
    "SeriesEvaluatorInputs",
    "SeriesRejectReason",
    "SeriesState",
    "SeriesSubType",
    "SingleGameProb",
    "TeamSide",
    "check_series_entry_risk",
    "classify_series_sub_type",
    "derive_single_game_prob",
    "evaluate_series_opportunity",
    "game_odds_from_metadata",
    "resolve_series_team",
    "series_state_from_metadata",
    "series_win_probability",
    "team_b_win_probability",
]
