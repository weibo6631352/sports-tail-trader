"""Outright 市场子包：championship / MVP / 转会 / 奖项等长周期 OUTRIGHT 市场。

与 ``current/trading/`` ``current/tail/`` 平行：不同 family 各自负责定价、风控、
分配和恢复，由顶层 ``strategy.py`` 按 ``descriptor.market_family`` 分派。
"""

from strategies.current.outright.types import (
    OutrightAction,
    OutrightEvaluation,
    OutrightRejectReason,
)
from strategies.current.outright.evaluator import evaluate_outright_opportunity
from strategies.current.outright.match import match_season_state, season_odds_from_metadata
from strategies.current.outright.pricing import (
    OutrightFairValue,
    outright_entry_price_cap,
    outright_exit_price_target,
    outright_fair_value,
)
from strategies.current.outright.risk import check_outright_entry_risk
from strategies.current.outright.team_resolver import resolve_market_team

__all__ = [
    "OutrightAction",
    "OutrightEvaluation",
    "OutrightFairValue",
    "OutrightRejectReason",
    "check_outright_entry_risk",
    "evaluate_outright_opportunity",
    "match_season_state",
    "outright_entry_price_cap",
    "outright_exit_price_target",
    "outright_fair_value",
    "resolve_market_team",
    "season_odds_from_metadata",
]
