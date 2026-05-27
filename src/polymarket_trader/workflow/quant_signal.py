"""量化信号入口——融合 Goalserve 赔率 / 数学概率公式 / 盘口 microprice。

quant_decider 通过 ``estimate_signal`` 拿到 [0.01, 0.99] 范围的结算概率
喂给 Kelly。要接入新量化信号源（自有 ML / 外部 API），在本模块扩展即可。

R6 改造（CPO Round 2 决议）：``estimate_signal`` 同时产出 ``LiveSignalSnapshot``
供 observability 链路记录（``/analytics/edge-signals`` 暴露 top-K 差价候选）。
**不**用 snapshot 直接驱动决策——决策仍由 prob_p → Kelly 主路径负责。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from polymarket_trader.domain.decisions import DecisionContext
from polymarket_trader.domain.signal_snapshot import LiveSignalSnapshot
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
    account_age_s: Decimal | None = None,
) -> tuple[Decimal, str, LiveSignalSnapshot]:
    """融合所有信号估算我方方向结算到 1.0 的真实概率 + 同步产出 observability snapshot。

    第 1 层：真概率信号（goalserve + 数学概率）max 融合——我方持仓 = 赢方，
    越高的真概率越接近真实结算 → 保护利润不被 stale 信号砸低 SELL 价。
    第 2 层：盘口信号（microprice / mid）— 真概率全缺时兜底，避免 spread
    大时拉高估值让 SELL 挂不出去。
    第 3 层：仅 bid 兜底。

    返回 ``(final_prob_p, source_used, snapshot)``——caller 用前两项继续决策，
    第三项交给 ``app.signal_snapshot_store`` 写入 ring buffer 供 endpoint 读。
    """

    # 各信号源单独评估——snapshot 全字段化（即使某层未被选用也记录）便于复盘。
    goalserve = goalserve_prob(context, token_id)
    math_p = math_prob(context, token_id)
    orderbook = context.orderbook
    microprice_val = orderbook.microprice if orderbook is not None else None

    # 第 1 层：真概率信号 max 融合。
    truth_candidates: list[tuple[Decimal, str]] = []
    if goalserve is not None:
        truth_candidates.append((goalserve, "goalserve_implied_prob"))
    if math_p is not None:
        truth_candidates.append((math_p, "math_prob"))

    if truth_candidates:
        best_value, best_source = max(truth_candidates, key=lambda x: x[0])
        final_prob = clamp_probability(best_value)
        source_used = best_source
    elif microprice_val is not None:
        # 第 2 层：盘口 microprice
        final_prob = clamp_probability(microprice_val)
        source_used = "microprice"
    elif best_ask is not None and best_ask > best_bid:
        # 第 2 层：mid 兜底
        final_prob = clamp_probability((best_bid + best_ask) / Decimal("2"))
        source_used = "market_mid"
    else:
        # 第 3 层：仅 bid 兜底
        final_prob = clamp_probability(best_bid)
        source_used = "best_bid"

    # edge_pp = pm_best_ask - goalserve_fair_prob：正值表示市场低估赢方
    # （Polymarket 价格低于博彩去 vig 真概率），是经典入场 edge 信号。
    # ask 或 goalserve 任一缺失 → 不算 edge（None），避免误报。
    edge_pp: Decimal | None = None
    if best_ask is not None and goalserve is not None:
        edge_pp = goalserve - best_ask

    snapshot = LiveSignalSnapshot(
        condition_id=context.market.condition_id if context.market is not None else "",
        token_id=token_id or "",
        pm_best_ask=best_ask,
        pm_best_bid=best_bid,
        goalserve_fair_prob=goalserve,
        math_prob=math_p,
        microprice=microprice_val,
        final_prob_p=final_prob,
        source_used=source_used,
        edge_pp=edge_pp,
        timestamp=datetime.now(timezone.utc),
        account_age_s=account_age_s,  # R15 (架构师 Round 7) bankroll staleness 归因
    )
    return final_prob, source_used, snapshot


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
    # P0 (架构师 Round 4 self-review)：mlb_totals UNDER (total>=line) 和 OVER
    # (n_half<=0+total<=line) 返 prob=1.0 + detail.outcome="lose" 表示我方 token
    # 100% 输；如果直接 fed Kelly 会被当 100% winner → 满仓 BUY 必输 token →
    # 爆仓。这是删 ENDED 短路 (R1 #9) 时漏掉的 lose 分支。
    if isinstance(result.details, Mapping) and result.details.get("outcome") == "lose":
        return None
    return result.probability


def goalserve_prob(
    context: DecisionContext,
    token_id: str | None,
) -> Decimal | None:
    """读取 Goalserve 盘口对我方方向的去 vig 真概率（fair_prob）。

    按 token 解析的盘口方向（OVER/UNDER/HOME/AWAY）选对应 Goalserve 字段：
    OVER/UNDER 走 goalserve_totals，HOME/AWAY 走 goalserve_moneyline，缺失时
    用 goalserve_spread 兜底。任意一步缺数据返回 None，调用侧退回市场中价。

    **优先读 `*_fair_prob`**（已去 overround，Σ=1 的真概率，喂给 Kelly 不会 overbet）；
    缺失时 fallback 到 raw `*_implied_prob`（含 vig，仅作向后兼容兜底）。
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
        base = "over" if side == SportsMarketSide.OVER else "under"
        suspended_key = f"{base}_suspended"
        return _fair_prob_field(totals, base, suspended_key)
    if side in (SportsMarketSide.HOME, SportsMarketSide.AWAY):
        base = "home" if side == SportsMarketSide.HOME else "away"
        suspended_key = f"{base}_suspended"
        moneyline = metadata.get("goalserve_moneyline")
        prob = _fair_prob_field(moneyline, base, suspended_key)
        if prob is not None:
            return prob
        return _fair_prob_field(metadata.get("goalserve_spread"), base, suspended_key)
    return None


def _fair_prob_field(
    market_odds: object,
    base_key: str,
    suspended_key: str,
) -> Decimal | None:
    """从 Goalserve 盘口 dict 取单方向**去 vig 真概率**。

    优先读 ``{base_key}_fair_prob``（live_state._devig_probs 已归一化，Σ=1）；
    缺失时 fallback 读 ``{base_key}_implied_prob``（raw，含 vig）。盘口/方向被暂停
    时视为无信号。

    举例：``base_key="home"`` → 先查 ``home_fair_prob``，缺则查 ``home_implied_prob``。
    """

    if not isinstance(market_odds, Mapping):
        return None
    if market_odds.get("suspended") or market_odds.get(suspended_key):
        return None
    raw = market_odds.get(f"{base_key}_fair_prob")
    if raw is None:
        raw = market_odds.get(f"{base_key}_implied_prob")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (TypeError, ValueError):
        return None
