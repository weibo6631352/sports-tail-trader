"""Outright 市场子包：championship / MVP / 转会 / 奖项等长周期 OUTRIGHT 市场。

与 ``current/trading/`` ``current/tail/`` 平行：不同 family 各自负责定价、风控、
分配和恢复，由顶层 ``strategy.py`` 按 ``descriptor.market_family`` 分派。
"""

from polymarket_trader.workflow.outright.types import (
    OutrightAction,
    OutrightEvaluation,
    OutrightRejectReason,
)
from polymarket_trader.workflow.outright.evaluator import evaluate_outright_opportunity
from polymarket_trader.workflow.outright.match import match_season_state, season_odds_from_metadata
from polymarket_trader.workflow.outright.pricing import (
    OutrightFairValue,
    outright_exit_price_target,
    outright_fair_value,
)
from polymarket_trader.workflow.outright.risk import check_outright_entry_risk
from polymarket_trader.workflow.outright.team_resolver import resolve_market_team
from polymarket_trader.workflow.outright.sizing import size_outright_entry
from polymarket_trader.workflow.outright.decide import decide_outright_entry, resolve_outright_reject_label

__all__ = [
    "OutrightAction",
    "OutrightEvaluation",
    "OutrightFairValue",
    "OutrightRejectReason",
    "check_outright_entry_risk",
    "decide_outright_entry",
    "evaluate_outright_opportunity",
    "match_season_state",
    "outright_exit_price_target",
    "outright_fair_value",
    "resolve_market_team",
    "resolve_outright_reject_label",
    "season_odds_from_metadata",
    "size_outright_entry",
]
