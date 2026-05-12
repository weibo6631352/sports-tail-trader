"""Series 市场子包：系列赛胜者 / 总局数 / 让分等系列赛盘口家族。

与 ``current/outright/`` ``current/tail/`` 平行：不同 family 各自负责子类型分类、
定价、风控和分配，由顶层 ``strategy.py`` 按 ``descriptor.market_family`` 分派。

本 worktree 仅落骨架：classifier + 类型 + record-only evaluator。实际定价模型在
后续 worktree（3-4）按子类型分别接入。
"""

from strategies.current.series.classifier import classify_series_sub_type
from strategies.current.series.evaluator import evaluate_series_opportunity
from strategies.current.series.types import (
    SeriesCandidate,
    SeriesEvaluation,
    SeriesRejectReason,
    SeriesState,
    SeriesSubType,
)

__all__ = [
    "SeriesCandidate",
    "SeriesEvaluation",
    "SeriesRejectReason",
    "SeriesState",
    "SeriesSubType",
    "classify_series_sub_type",
    "evaluate_series_opportunity",
]
