"""量化信号入口——融合 Goalserve 赔率 / 数学概率公式 / 盘口 microprice。

quant_decider 通过 ``estimate_signal`` 拿到 [0.01, 0.99] 范围的结算概率
喂给 Kelly。要接入新量化信号源（自有 ML / 外部 API），在本模块扩展即可。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from polymarket_trader.domain.decisions import DecisionContext

from polymarket_trader.workflow.outcomes import target_for_token
from polymarket_trader.sports import SportsMarketSide

# 概率 clamp 边界：极端 0/1 会让 Kelly / 止盈止损判断失真。
_PROB_MIN = Decimal("0.01")
_PROB_MAX = Decimal("0.99")


def clamp_probability(value: Decimal) -> Decimal:
    """把概率收敛到 [0.01, 0.99]。"""

    return min(_PROB_MAX, max(_PROB_MIN, value))


def estimate_signal(
    context: DecisionContext,
    *,
    token_id: str | None,
    best_bid: Decimal,
    best_ask: Decimal | None,
) -> tuple[Decimal, str]:
    """融合所有信号估算我方方向结算到 1.0 的真实概率。

    第 1 层：真概率信号（goalserve + 数学概率）max 融合——我方持仓 = 赢方，
    越高的真概率越接近真实结算 → 保护利润不被 stale 信号砸低 SELL 价。
    第 2 层：盘口信号（microprice / mid）— 真概率全缺时兜底，避免 spread
    大时拉高估值让 SELL 挂不出去。
    第 3 层：仅 bid 兜底。
    """

    # 第 1 层：真概率信号 max 融合。
    truth_candidates: list[tuple[Decimal, str]] = []
    goalserve = goalserve_prob(context, token_id)
    if goalserve is not None:
        truth_candidates.append((goalserve, "goalserve_implied_prob"))
    math_p = math_prob(context, token_id)
    if math_p is not None:
        truth_candidates.append((math_p, "math_prob"))
    if truth_candidates:
        best_value, best_source = max(truth_candidates, key=lambda x: x[0])
        return clamp_probability(best_value), best_source

    # 第 2 层：盘口 microprice / mid 兜底。
    orderbook = context.orderbook
    if orderbook is not None:
        microprice = orderbook.microprice
        if microprice is not None:
            return clamp_probability(microprice), "microprice"
    if best_ask is not None and best_ask > best_bid:
        mid = (best_bid + best_ask) / Decimal("2")
        return clamp_probability(mid), "market_mid"

    # 第 3 层：仅 bid 兜底。
    return clamp_probability(best_bid), "best_bid"


def math_prob(
    context: DecisionContext,
    token_id: str | None,
) -> Decimal | None:
    """数学概率公式——单场 sport-specific（baseball/soccer/basketball/tennis/
    hockey/cricket）。无对应公式返回 None，让上层 fallback goalserve 或 microprice。
    """

    from polymarket_trader.sports.math_prob import evaluate_math_prob
    from polymarket_trader.sports.parsing import live_game_state_from_metadata
    from polymarket_trader.workflow.outcomes import describe_sports_market

    market = context.market
    if market is None or token_id is None:
        return None
    descriptor = describe_sports_market(market)
    if not descriptor.accepted or descriptor.market_type is None:
        return None
    target = target_for_token(market, token_id)
    if target is None:
        return None
    game = live_game_state_from_metadata(context.metadata)
    if game is None:
        return None
    result = evaluate_math_prob(
        descriptor.market_type,
        target.side,
        descriptor.line,
        game,
        market_slug=market.market_slug,
    )
    if result.method == "unsupported" or result.probability <= Decimal("0"):
        return None
    return result.probability


def goalserve_prob(
    context: DecisionContext,
    token_id: str | None,
) -> Decimal | None:
    """读取 Goalserve 盘口对我方方向的隐含概率。

    按 token 解析的盘口方向（OVER/UNDER/HOME/AWAY）选对应 Goalserve 字段：
    OVER/UNDER 走 goalserve_totals，HOME/AWAY 走 goalserve_moneyline，缺失时
    用 goalserve_spread 兜底。任意一步缺数据返回 None，调用侧退回市场中价。
    """

    if context.market is None or token_id is None:
        return None
    target = target_for_token(context.market, token_id)
    if target is None:
        return None
    side = target.side
    metadata = context.metadata or {}
    if side in (SportsMarketSide.OVER, SportsMarketSide.UNDER):
        totals = metadata.get("goalserve_totals")
        key = "over_implied_prob" if side == SportsMarketSide.OVER else "under_implied_prob"
        suspended_key = "over_suspended" if side == SportsMarketSide.OVER else "under_suspended"
        return _implied_prob_field(totals, key, suspended_key)
    if side in (SportsMarketSide.HOME, SportsMarketSide.AWAY):
        key = "home_implied_prob" if side == SportsMarketSide.HOME else "away_implied_prob"
        suspended_key = "home_suspended" if side == SportsMarketSide.HOME else "away_suspended"
        moneyline = metadata.get("goalserve_moneyline")
        prob = _implied_prob_field(moneyline, key, suspended_key)
        if prob is not None:
            return prob
        return _implied_prob_field(metadata.get("goalserve_spread"), key, suspended_key)
    return None


def _implied_prob_field(
    market_odds: object,
    key: str,
    suspended_key: str,
) -> Decimal | None:
    """从 Goalserve 盘口 dict 取单方向 implied_prob，盘口/方向被暂停时视为无信号。"""

    if not isinstance(market_odds, Mapping):
        return None
    if market_odds.get("suspended") or market_odds.get(suspended_key):
        return None
    raw = market_odds.get(key)
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (TypeError, ValueError):
        return None
