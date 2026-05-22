"""赔率差价（odds-gap）入场评估。

这是扫尾锁定（tail-lock）之外的第二条入场路径（CLAUDE.md §10/§17）：当 Goalserve
盘中博彩盘口对某一方向的**去抽水真实概率**显著高于 Polymarket 买入 ask 时，说明
Polymarket 定价落后于博彩市场，这一差价（edge）本身就是可交易优势。

与扫尾锁定的区别：
- 扫尾锁定依赖"比赛状态证明结果已数学锁定"，结算概率≈1.0。
- 赔率差价是**概率性入场**：依赖"博彩市场比 Polymarket 更早定价"的效率差，
  真实概率是去抽水后的 true_p（远小于 1.0），有输的可能。
- 因此赔率差价**必须保留流动性/价差门禁**——判断错时需要退出通道。

去抽水（de-vig）是正确性关键：Goalserve 的 ``*_implied_prob`` 来自 ``1/eu``，
跨双方相加 >1（博彩 overround/抽水约 5-8%）。直接用原始 implied 会在每个市场上
凭空多算约 5% edge。2-way 市场归一：``true_p(side) = implied(side) /
(implied(home) + implied(away))``。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from strategies.sports_framework import SportsMarketSide, SportsMarketType

from .types import SportsTailCandidate, TailEvaluation, TailPolicy
from .core import _accept, _reject
from .types import SportsTailOpportunityType, TailRejectReason


@dataclass(frozen=True, slots=True)
class _DevigResult:
    """2-way 盘口去抽水结果。"""

    home_true_p: Decimal
    away_true_p: Decimal
    overround: Decimal  # implied 之和（>1 表示存在抽水）


def _to_decimal(value: Any) -> Decimal | None:
    """把 Goalserve metadata 里的数值字段安全转 Decimal。

    赔率/概率字段可能是 float（来自 ``1/eu`` 计算）或字符串；非法值返回 None，
    由调用侧视为"无可用赔率信号"，不阻塞其它入场路径。
    """

    if value is None:
        return None
    try:
        # 经 str() 中转避免 float 二进制误差直接进 Decimal。
        result = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not result.is_finite():
        return None
    return result


def _devig_two_way(
    home_implied: Decimal,
    away_implied: Decimal,
) -> _DevigResult | None:
    """对 2-way 盘口的原始 implied 概率去抽水。

    两侧 implied 必须均为正且和为正；任一不满足返回 None（赔率不可信）。
    """

    if home_implied <= Decimal("0") or away_implied <= Decimal("0"):
        return None
    overround = home_implied + away_implied
    if overround <= Decimal("0"):
        return None
    return _DevigResult(
        home_true_p=home_implied / overround,
        away_true_p=away_implied / overround,
        overround=overround,
    )


def _goalserve_moneyline_devig(
    metadata: Mapping[str, Any],
) -> _DevigResult | None:
    """从盘口快照 metadata 里的 ``goalserve_moneyline`` 提取去抽水真实概率。

    返回 None 表示无 Goalserve Money Line 赔率、盘口被暂停、或赔率不可信——
    调用侧据此判定"无赔率差价信号"，不影响扫尾锁定路径。
    """

    gs = metadata.get("goalserve_moneyline")
    if not isinstance(gs, dict):
        return None
    # 整体盘口暂停时赔率不反映真实概率，不可用于差价判断。
    if gs.get("suspended"):
        return None
    home_implied = _to_decimal(gs.get("home_implied_prob"))
    away_implied = _to_decimal(gs.get("away_implied_prob"))
    if home_implied is None or away_implied is None:
        return None
    return _devig_two_way(home_implied, away_implied)


def _moneyline_side_suspended(
    metadata: Mapping[str, Any],
    side: SportsMarketSide,
) -> bool:
    """判断 Goalserve Money Line 中我方对应一侧是否被单独暂停。"""

    gs = metadata.get("goalserve_moneyline")
    if not isinstance(gs, dict):
        return False
    if side == SportsMarketSide.HOME:
        return bool(gs.get("home_suspended"))
    if side == SportsMarketSide.AWAY:
        return bool(gs.get("away_suspended"))
    return False


def evaluate_odds_gap_opportunity(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估候选是否构成赔率差价入场机会。

    仅在 Money Line（2-way HOME/AWAY）盘口上判断：Goalserve Money Line 赔率是
    2-way，去抽水语义干净，与 Polymarket 单边 ask 直接可比。

    调用约定：本函数只在扫尾锁定**未命中**后调用；价格/流动性/价差等通用门禁
    已由 ``_common_reject_reason`` 通过——赔率差价是概率性入场，必须保留这些门禁
    （判断错时要有退出通道），不旁路。

    Totals/Spreads 暂不纳入：Goalserve totals/spread 虽也是 2-way，但其
    over/under 与 home/away 让分的盘口线（line/handicap）必须与 Polymarket 市场线
    完全一致，差价才可比；线不一致时概率不可直接比较。该校线逻辑是独立后续工作。
    """

    market = candidate.market

    # 赔率差价当前只覆盖 Money Line：Goalserve ML 2-way 去抽水语义最干净。
    if market.market_type != SportsMarketType.MONEYLINE:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    if market.best_ask is None:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    # 我方一侧被单独暂停 → 该侧赔率不反映真实概率，不可入场。
    if _moneyline_side_suspended(market.metadata, market.side):
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    devig = _goalserve_moneyline_devig(market.metadata)
    if devig is None:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    if market.side == SportsMarketSide.HOME:
        true_p = devig.home_true_p
    else:
        true_p = devig.away_true_p

    # edge = 去抽水真实概率 − Polymarket 买入 ask。
    edge = true_p - market.best_ask
    if edge < policy.odds_gap_min_edge:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    # 把去抽水概率与 edge 写入候选 metadata：供 Kelly 用真实 prob_p 定注、供审计复盘。
    enriched = SportsTailCandidate(
        game=candidate.game,
        market=candidate.market,
        reason="odds_gap_entry",
        risk_notes=candidate.risk_notes,
        metadata={
            **candidate.metadata,
            "odds_gap_true_p": str(true_p),
            "odds_gap_edge": str(edge),
            "odds_gap_best_ask": str(market.best_ask),
            "odds_gap_overround": str(devig.overround),
            "odds_gap_side": market.side.value,
        },
    )
    return _accept(
        enriched,
        "odds_gap_entry",
        policy.odds_gap_execution_permission,
        opportunity_type=SportsTailOpportunityType.ODDS_GAP,
    )
