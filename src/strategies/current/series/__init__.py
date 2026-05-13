"""Series 市场子包：系列赛胜者 / 总局数 / 让分等系列赛盘口家族。

与 ``current/outright/`` ``current/tail/`` 平行：不同 family 各自负责子类型分类、
定价、风控和分配，由顶层 ``strategy.py`` 按 ``descriptor.market_family`` 分派。

子类型与定价模型对应：
- WINNER → ``winner_model.series_win_probability``（负二项闭式）
- TOTAL_GAMES → ``total_games_model.total_games_distribution`` + Over/Under 累加
- GAME_HANDICAP → series scope 走 ``handicap_model.series_handicap_cover_probability``；
  single_game scope 走 ``handicap_model.single_game_cover_probability``（spread de-vig）

所有定价路径都流经 ``_shared/edge_gates.check_entry_gates`` 与 ``series/risk.check_series_entry_risk``，
再由 strategy 投影成 ``ExtensionDecision``。
"""

from strategies.current.series.classifier import classify_series_sub_type
from strategies.current.series.decide import (
    decide_series_entry,
    resolve_series_reject_label,
    resolve_series_sub_type_label,
)
from strategies.current.series.evaluator import (
    SeriesEvaluatorInputs,
    evaluate_series_opportunity,
)
from strategies.current.series.handicap_model import (
    series_handicap_cover_probability,
    single_game_cover_probability,
)
from strategies.current.series.handicap_outcome import (
    HandicapBet,
    HandicapScope,
    parse_handicap_outcome,
)
from strategies.current.series.match import (
    GameOdds,
    GameSpreads,
    game_odds_from_metadata,
    game_spreads_from_metadata,
    series_state_from_metadata,
)
from strategies.current.series.risk import (
    SeriesSubTypeRiskConfig,
    check_series_entry_risk,
)
from strategies.current.series.single_game_prob import (
    SingleGameProb,
    derive_single_game_prob,
)
from strategies.current.series.team_resolver import TeamSide, resolve_series_team
from strategies.current.series.total_games_model import (
    prob_over,
    prob_under,
    push_probability,
    total_games_distribution,
)
from strategies.current.series.total_games_outcome import (
    Direction,
    parse_total_games_outcome,
)
from strategies.current.series.types import (
    SeriesCandidate,
    SeriesEvaluation,
    SeriesRejectReason,
    SeriesState,
    SeriesSubType,
)
from strategies.current.series.sizing import (
    SeriesSubTypeSettings,
    series_subtype_settings,
    size_series_entry,
)
from strategies.current.series.winner_model import (
    series_win_probability,
    team_b_win_probability,
)

__all__ = [
    "Direction",
    "GameOdds",
    "GameSpreads",
    "HandicapBet",
    "HandicapScope",
    "SeriesCandidate",
    "SeriesEvaluation",
    "SeriesEvaluatorInputs",
    "SeriesRejectReason",
    "SeriesState",
    "SeriesSubType",
    "SeriesSubTypeRiskConfig",
    "SeriesSubTypeSettings",
    "SingleGameProb",
    "TeamSide",
    "check_series_entry_risk",
    "classify_series_sub_type",
    "decide_series_entry",
    "derive_single_game_prob",
    "evaluate_series_opportunity",
    "game_odds_from_metadata",
    "game_spreads_from_metadata",
    "parse_handicap_outcome",
    "parse_total_games_outcome",
    "prob_over",
    "prob_under",
    "push_probability",
    "resolve_series_reject_label",
    "resolve_series_sub_type_label",
    "resolve_series_team",
    "series_handicap_cover_probability",
    "series_state_from_metadata",
    "series_subtype_settings",
    "series_win_probability",
    "single_game_cover_probability",
    "size_series_entry",
    "team_b_win_probability",
    "total_games_distribution",
]
