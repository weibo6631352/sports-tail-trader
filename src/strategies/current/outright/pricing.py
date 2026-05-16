"""Outright 反向定价：以赛季隐含概率为锚，输出 fair_value 与 exit 价位。

纯函数：输入 SeasonOddsSnapshot + Market + 配置，输出 fair_value 与
exit_target。``OutrightFairValue`` 同时携带 value 与可审计的拒绝原因
（团队解析失败 / 赛季概率不归一 / outcome 名不在 snapshot 中），让 evaluator 不
丢失上下文。entry_price_cap 由 ``_shared/edge_gates.entry_price_cap`` 共享，
与 series 共用同一份公式。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot

from strategies.current.outright.team_resolver import resolve_market_team
from strategies.current.outright.types import OutrightRejectReason

_PRICE_FLOOR = Decimal("0.01")
_PRICE_CEILING = Decimal("0.99")
# de-vig 概率合理区间：sum 应≈1，允许 ±5% 误差吸收四舍五入与小数源差。
_SIGMA_LOWER = Decimal("0.95")
_SIGMA_UPPER = Decimal("1.05")
_BINARY_OUTCOMES = frozenset({"yes", "no"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutrightFairValue:
    """定价结果：value 命中时 reject 为 None；不命中时 reject 给出可审计原因。"""

    value: Decimal | None
    reject: OutrightRejectReason | None = None


def outright_fair_value(
    snapshot: SeasonOddsSnapshot,
    market: Market,
    outcome_label: str,
) -> OutrightFairValue:
    """按 outcome 名找 fair probability，YES/NO 二元市场走团队反向匹配。

    1) 类别型 outright：outcome 直接是球队名 → snapshot 字典命中即返回。
    2) 二元 YES/NO：先看 snapshot.fair_probabilities 总和是否在 [0.95, 1.05]
       内——不在视为 SEASON_ODDS_INCOMPLETE；再走 ``resolve_market_team``，
       命中 → YES 返回 p、NO 返回 1-p；不命中 → OUTRIGHT_TEAM_NOT_RESOLVED。
    3) 既非类别命中也非 YES/NO → ODDS_OUTCOME_NOT_MAPPED（保留旧契约）。
    """

    if snapshot is None:
        return OutrightFairValue(value=None, reject=OutrightRejectReason.MISSING_SEASON_ODDS)
    direct = snapshot.probability_for(outcome_label)
    if direct is not None and direct > 0:
        return OutrightFairValue(value=_clamp(direct))
    normalized_label = outcome_label.strip().lower()
    if normalized_label not in _BINARY_OUTCOMES:
        return OutrightFairValue(value=None, reject=OutrightRejectReason.ODDS_OUTCOME_NOT_MAPPED)
    sigma = sum(snapshot.fair_probabilities.values(), Decimal("0"))
    if sigma < _SIGMA_LOWER or sigma > _SIGMA_UPPER:
        return OutrightFairValue(value=None, reject=OutrightRejectReason.SEASON_ODDS_INCOMPLETE)
    team_key = resolve_market_team(market, snapshot)
    if team_key is None:
        return OutrightFairValue(value=None, reject=OutrightRejectReason.OUTRIGHT_TEAM_NOT_RESOLVED)
    team_p = snapshot.fair_probabilities[team_key]
    if normalized_label == "yes":
        return OutrightFairValue(value=_clamp(team_p))
    # NO = 1 - p；clamp 保证落在 [floor, ceiling]，包括 team_p 极端值场景。
    return OutrightFairValue(value=_clamp(Decimal("1") - team_p))


def outright_exit_price_target(
    fair_value: Decimal,
    entry_price: Decimal,
    *,
    exit_edge_target: Decimal,
    min_profit_per_share: Decimal,
) -> Decimal:
    """退出目标价：fair_value 上方留 buffer 平仓，或至少高于入场价 + 最小利润。"""

    if min_profit_per_share <= 0:
        raise ValueError(f"min_profit_per_share must be positive, got {min_profit_per_share}")
    target_by_edge = fair_value + exit_edge_target
    target_by_floor = entry_price + min_profit_per_share
    result = _clamp(max(target_by_edge, target_by_floor))
    if result <= entry_price:
        raise ValueError(
            f"exit_price_target={result} <= entry_price={entry_price}; "
            f"fair_value={fair_value} exit_edge={exit_edge_target} min_profit={min_profit_per_share}"
        )
    return result


def _clamp(value: Decimal) -> Decimal:
    if value < _PRICE_FLOOR:
        logger.warning("outright_fair_value_clamped", extra={"raw": str(value), "clamped": str(_PRICE_FLOOR)})
        return _PRICE_FLOOR
    if value > _PRICE_CEILING:
        logger.warning("outright_fair_value_clamped", extra={"raw": str(value), "clamped": str(_PRICE_CEILING)})
        return _PRICE_CEILING
    return value


__all__ = [
    "OutrightFairValue",
    "outright_fair_value",
    "outright_exit_price_target",
]
