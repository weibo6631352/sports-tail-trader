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

覆盖的盘口类型：
- MONEYLINE：2-way HOME/AWAY，无盘口线，去抽水后直接与 ask 可比。
- TOTALS：2-way OVER/UNDER。**只有当 Goalserve totals 的 total_line 与 Polymarket
  market.line 完全一致、且两者范围（scope）同为整场时**，去抽水概率才与 ask 可比。
- SPREADS：2-way HOME/AWAY 让分盘，同样要求让分线 + 范围一致。

线/范围对账（reconciliation）是 totals/spread 的正确性关键：moneyline 没有盘口线，
home/away 语义天然对齐；但一个 over 9.5 的赔率不能拿去和一个 over 8.5 的 Polymarket
市场比——两者结算条件不同，概率不可比。线或范围对不上时**不产出差价信号**（既不
凭空造 edge，也不阻塞其它入场路径）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from strategies.sports_framework import (
    SportsMarketScopeType,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    market_scope,
)

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
    *,
    draw_implied: Decimal | None = None,
) -> _DevigResult | None:
    """对盘口的原始 implied 概率去抽水。

    统一走 N-way power method（domain/devig.py）。三结果运动（足球整场）必须传
    ``draw_implied``——否则 2-way devig 会丢掉 draw 的 vig 份额，导致 home/away
    被高估 0.15-0.20（举例 home_eu=2.0 away_eu=4.0 draw_eu=3.0：2-way 给 home
    true_p=0.667，3-way 给 0.462）。

    任一 implied <= 0 或求和不可用时返回 None（赔率不可信）。
    """
    from polymarket_trader.domain.devig import devig_implied, overround as _ovr

    if home_implied <= Decimal("0") or away_implied <= Decimal("0"):
        return None
    raw: dict[str, Decimal] = {"home": home_implied, "away": away_implied}
    if draw_implied is not None and draw_implied > Decimal("0"):
        raw["draw"] = draw_implied
    fair = devig_implied(raw)
    if not fair or fair["home"] <= Decimal("0") or fair["away"] <= Decimal("0"):
        return None
    return _DevigResult(
        home_true_p=fair["home"],
        away_true_p=fair["away"],
        overround=_ovr(raw),
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
    # 3-way（含 draw）必须传 draw_implied 让 N-way devig 正确归一；soccer 整场
    # ML 不修这个会被 2-way 错算高估 0.20。2-way 运动留 None 自动退化到 2 个结果。
    draw_implied = _to_decimal(gs.get("draw_implied_prob"))
    return _devig_two_way(home_implied, away_implied, draw_implied=draw_implied)


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


def _totals_side_suspended(metadata: Mapping[str, Any], side: SportsMarketSide) -> bool:
    """判断 Goalserve totals 中我方对应一侧（over/under）是否被单独暂停。"""

    gs = metadata.get("goalserve_totals")
    if not isinstance(gs, dict):
        return False
    if side == SportsMarketSide.OVER:
        return bool(gs.get("over_suspended"))
    if side == SportsMarketSide.UNDER:
        return bool(gs.get("under_suspended"))
    return False


def _spread_side_suspended(metadata: Mapping[str, Any], side: SportsMarketSide) -> bool:
    """判断 Goalserve spread 中我方对应一侧（home/away）是否被单独暂停。"""

    gs = metadata.get("goalserve_spread")
    if not isinstance(gs, dict):
        return False
    if side == SportsMarketSide.HOME:
        return bool(gs.get("home_suspended"))
    if side == SportsMarketSide.AWAY:
        return bool(gs.get("away_suspended"))
    return False


def _goalserve_is_full_game_segment(market_name: Any) -> bool:
    """判断 Goalserve totals/spread 盘口名是否指向整场（full-game）范围。

    Goalserve 的 ``_extract_goalserve_totals`` / ``_extract_goalserve_spread`` 已在
    抽取阶段过滤掉 "2nd half" / "quarter" 盘口，但盘口名仍可能携带其它子周期标记
    （如电竞的 "Total (map 2)"、"Total (set 3)"）。这些子周期赔率不能与整场
    Polymarket 市场比较，必须在此二次确认。
    """

    if not isinstance(market_name, str):
        # 名称缺失时无法证明范围一致——保守判定为非整场。
        return False
    lowered = market_name.lower()
    # 出现任一子周期标记（map/set/quarter/half/period/inning/leg）即非整场。
    sub_period_markers = (
        "map ",
        "(map",
        "set ",
        "(set",
        "quarter",
        "1st half",
        "2nd half",
        "half ",
        "period",
        "inning",
        "leg ",
        "(leg",
    )
    return not any(marker in lowered for marker in sub_period_markers)


def _line_matches(goalserve_line: Any, market_line: Decimal | None) -> bool:
    """对账 Goalserve 盘口线与 Polymarket 市场线：必须均存在且数值相等。

    Goalserve 的线是字符串（如 ``"9.5"`` / ``"-3.5"``），经 str()→Decimal 解析后
    与 Polymarket 的 ``market.line``（Decimal）做精确比较。任一缺失或解析失败即
    判定不匹配——概率不可比，不产出差价信号。
    """

    if market_line is None:
        return False
    parsed = _to_decimal(goalserve_line)
    if parsed is None:
        return False
    return parsed == market_line


def _is_late_game_for_underdog_buy(game: Any) -> bool:
    """判定比赛是否进入末段（胜负将快速收敛，underdog 翻盘概率极低）。

    跨运动末段定义（实战经验阈值）：
    - baseball: inning >= 8（最后 2 局多数翻盘已不可能）
    - basketball: 4th quarter（current_period >= 4）
    - tennis: 决胜盘（current_set 等于 best_of；best_of 未知时第 3 盘起算）
    - soccer: 80+ 分钟（second_half 且 clock_minutes >= 35）
    - 其他运动暂不判定（无统一末段语义）→ 返回 False（不触发该守卫）
    """
    sport = (getattr(game, "sport", "") or "").lower()
    if sport == "baseball":
        st = getattr(game, "baseball_state", None)
        if st and (st.current_inning or 0) >= 8:
            return True
    elif sport in ("basketball", "basket"):
        st = getattr(game, "basketball_state", None)
        if st and (st.current_period or 0) >= 4:
            return True
    elif sport == "tennis":
        st = getattr(game, "tennis_state", None)
        if st:
            cur_set = st.current_set or 0
            best_of = getattr(st, "best_of", None) or 0
            # 决胜盘：BO3 第 3 盘 / BO5 第 5 盘；best_of 未知时第 3 盘起算
            if best_of > 0 and cur_set >= best_of:
                return True
            if best_of == 0 and cur_set >= 3:
                return True
    elif sport == "soccer":
        st = getattr(game, "soccer_state", None)
        if st:
            period = (st.period or "").lower()
            clock = st.clock_minutes or 0
            # second_half 且 35+ 分钟（=正赛 80+ 分钟，含可能补时）
            if "second" in period and clock >= 35:
                return True
            if "extra" in period or "penalt" in period:
                return True
    return False


def evaluate_odds_gap_opportunity(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估候选是否构成赔率差价入场机会,按盘口类型分派。

    调用约定:本函数只在扫尾锁定**未命中**后调用;价格/流动性/价差等通用门禁
    已由 ``_common_reject_reason`` 通过——赔率差价是概率性入场,必须保留这些门禁
    (判断错时要有退出通道),不旁路。

    **末段守卫**: 比赛进入最后阶段时 Goalserve odds 已 stale(博彩 book 在末段
    不更新或停盘),不应基于此信号入场。具体见 _is_odds_stale_late_game。

    分派:
    - MONEYLINE → ``_evaluate_moneyline_odds_gap``(无盘口线,去抽水直接可比)。
    - TOTALS    → ``_evaluate_totals_odds_gap``(需 over/under 线 + 范围对账)。
    - SPREADS   → ``_evaluate_spreads_odds_gap``(需让分线 + 范围对账)。
    """

    market = candidate.market
    # live feed stale 守门：feed 数据陈旧时不入场——陈旧数据可能让我们买在
    # 已锁定输方（goalserve odds 也是过时的，devig edge 失真）。阈值由
    # policy.odds_gap_max_live_feed_lag_seconds 控制（默认 15s）。
    lag = candidate.game.live_feed_lag_seconds()
    if lag is not None and lag > policy.odds_gap_max_live_feed_lag_seconds:
        return _reject(
            candidate,
            TailRejectReason.LIVE_FEED_STALE.value,
            metadata={
                "live_feed_lag_seconds": str(round(lag, 1)),
                "max_lag_threshold_seconds": policy.odds_gap_max_live_feed_lag_seconds,
            },
        )
    # 流动性守卫: ask 深度 < odds_gap_min_liquidity_usdc 的小盘口禁用 odds_gap 入场。
    # 用户要求:"资金太小的盘口,只跑尾盘"——冷盘口退出难,odds_gap 概率性入场
    # 一旦判断错没退出通道,只能 hold 到结算。tail lockin(数学锁定)不依赖流动性,
    # 但 odds_gap 是概率性,流动性是必要条件。
    if market.buyable_liquidity_usdc < policy.odds_gap_min_liquidity_usdc:
        return _reject(
            candidate,
            TailRejectReason.NO_ODDS_GAP.value,
            metadata={
                "odds_gap_reject_reason": "low_liquidity_only_lockin",
                "buyable_liquidity_usdc": str(market.buyable_liquidity_usdc),
                "min_liquidity_threshold": str(policy.odds_gap_min_liquidity_usdc),
            },
        )
    # 负 EV 价位守卫已挪到 _accept_odds_gap（算完 devig 后再判定，只拦 edge 不够
    # 大的中间价位入场。0.40-0.60 区间需要 edge >= 0.15 才放过——单纯算出 edge
    # 不够区分"真 edge"和"odds 噪音"，实证数据 416 笔显示该区间累计 -$45）。
    # 无退出通道守卫: odds_gap 是概率性入场,best_bid=None 表示盘口无人接 SELL,
    # 错判时无法主动平仓只能 hold 到结算 / 亏到 0。tail lockin 不受此约束（数学
    # 锁定可以等结算）,但 odds_gap 必须有退出。
    if market.best_bid is None:
        return _reject(
            candidate,
            TailRejectReason.NO_EXIT_CHANNEL.value,
            metadata={
                "best_ask": str(market.best_ask) if market.best_ask is not None else None,
                "ask_depth": str(market.buyable_liquidity_usdc),
                "guard_scope": "odds_gap_no_best_bid",
            },
        )
    # 宽 spread 守卫: best_bid 离 best_ask 太远 → 入场后立刻成"账面亏损"(BUY ask
    # 但 mark-to-market 用 bid),且退出价远低于入场价。实战案例: BUY @ 0.27 →
    # best_bid=0.18 → 浮亏立刻 -33%(还没价格反向)。spread > 30% 直接拒入场,
    # 留给 lockin 这种不依赖出场的路径。
    if market.best_ask is not None and market.best_ask > Decimal("0"):
        spread_ratio = (market.best_ask - market.best_bid) / market.best_ask
        if spread_ratio > Decimal("0.30"):
            return _reject(
                candidate,
                TailRejectReason.NO_EXIT_CHANNEL.value,
                metadata={
                    "best_bid": str(market.best_bid),
                    "best_ask": str(market.best_ask),
                    "spread_ratio": str(spread_ratio.quantize(Decimal("0.001"))),
                    "guard_scope": "odds_gap_spread_too_wide",
                },
            )
    # 末段低赔率 underdog 守卫: 比赛进入收敛段时(baseball 8th+ inning /
    # basket 4th quarter 末段 / tennis 决胜盘 / soccer 80+ min), BUY price
    # < 0.30 = underdog 一方, 翻盘概率极低 → 几乎必砸手里, 不下单。
    # 该规则不影响 lockin (已锁定盘口本就接近 1.0, 不受 0.30 阈值影响)。
    if market.best_ask is not None and market.best_ask < Decimal("0.30"):
        if _is_late_game_for_underdog_buy(candidate.game):
            return _reject(
                candidate,
                TailRejectReason.LATE_GAME_LOW_PRICE_UNDERDOG.value,
                metadata={
                    "best_ask": str(market.best_ask),
                    "sport": candidate.game.sport,
                    "period": candidate.game.period,
                },
            )
    # 盘口风向守卫: 10s 窗口 OFI/microprice/momentum 综合方向 = "no"(看跌) 且
    # confidence >= 0.5 → 此时入场会落地即亏。entry 必须看盘口风向, 不能只看
    # best_ask < fair_value——后者在 ask 厚 bid 薄、microprice 快速下走时仍触发,
    # 实战案例: BUY @ 0.31 → 20s 后 SELL @ 0.26 (-16%)。
    ob_dir = market.metadata.get("orderbook_direction")
    if isinstance(ob_dir, dict):
        label = str(ob_dir.get("direction_label") or "")
        try:
            confidence = Decimal(str(ob_dir.get("confidence") or "0"))
        except (ArithmeticError, ValueError, TypeError):
            confidence = Decimal("0")
        if label == "no" and confidence >= Decimal("0.5"):
            return _reject(
                candidate,
                TailRejectReason.ORDERBOOK_DIRECTION_BEARISH.value,
                metadata={
                    "direction_label": label,
                    "confidence": str(confidence),
                    "direction_score": str(ob_dir.get("direction_score") or ""),
                    "flow_imbalance": str(ob_dir.get("flow_imbalance") or ""),
                },
            )
    if market.market_type == SportsMarketType.MONEYLINE:
        return _evaluate_moneyline_odds_gap(candidate, policy)
    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_totals_odds_gap(candidate, policy)
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_spreads_odds_gap(candidate, policy)
    return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)


def _evaluate_moneyline_odds_gap(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """Money Line 赔率差价：Goalserve ML 2-way 去抽水后直接与单边 ask 可比。

    Money Line 无盘口线，home/away 语义天然对齐，无需线/范围对账。
    """

    market = candidate.market

    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    if market.best_ask is None:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    # Goalserve Money Line 赔率是整场范围；分段 ML（单节/半场/分节）结算条件不同，
    # 概率不可比——分段 scope 直接判定无差价信号，不凭空造 edge。
    if market_scope(market).scope_type != SportsMarketScopeType.FULL_GAME:
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

    return _accept_odds_gap(candidate, policy, true_p, devig.overround)


def _evaluate_totals_odds_gap(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """Totals（Over/Under）赔率差价：去抽水前必须先做盘口线 + 范围对账。

    只有当 Goalserve totals 的 ``total_line`` 与 Polymarket ``market.line`` 相等、
    且两者范围同为整场时，去抽水的 over/under 概率才与 ask 可比。线或范围对不上
    时返回 ``odds_gap_line_mismatch``——概率不可比，不凭空造 edge。
    """

    market = candidate.market

    if market.side not in {SportsMarketSide.OVER, SportsMarketSide.UNDER}:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    if market.best_ask is None:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    gs = market.metadata.get("goalserve_totals")
    if not isinstance(gs, dict):
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    # 整体盘口暂停 → 赔率不反映真实概率。
    if gs.get("suspended"):
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    # 我方一侧被单独暂停 → 不可入场。
    if _totals_side_suspended(market.metadata, market.side):
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    # 线/范围对账：任一不一致则概率不可比，单独标记便于审计。
    if not _line_matches(gs.get("total_line"), market.line):
        return _reject(candidate, TailRejectReason.ODDS_GAP_LINE_MISMATCH.value)
    if not _totals_scope_matches(market, gs.get("market_name")):
        return _reject(candidate, TailRejectReason.ODDS_GAP_LINE_MISMATCH.value)

    over_implied = _to_decimal(gs.get("over_implied_prob"))
    under_implied = _to_decimal(gs.get("under_implied_prob"))
    if over_implied is None or under_implied is None:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    devig = _devig_two_way(over_implied, under_implied)
    if devig is None:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    # _DevigResult 的 home/away 字段在此承载 over/under：第一参数=over，第二=under。
    if market.side == SportsMarketSide.OVER:
        true_p = devig.home_true_p
    else:
        true_p = devig.away_true_p

    return _accept_odds_gap(candidate, policy, true_p, devig.overround)


def _evaluate_spreads_odds_gap(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """Spreads（让分盘）赔率差价：去抽水前必须先做让分线 + 范围对账。

    只有当 Goalserve spread 的让分线与 Polymarket ``market.line`` 相等、且两者
    范围同为整场时，去抽水的 home/away 概率才与 ask 可比。
    """

    market = candidate.market

    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    if market.best_ask is None:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    gs = market.metadata.get("goalserve_spread")
    if not isinstance(gs, dict):
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    if gs.get("suspended"):
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    if _spread_side_suspended(market.metadata, market.side):
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    # 让分线对账：Polymarket 让分盘的 market.line 是我方一侧的让分；与 Goalserve
    # 对应侧的 handicap 比较（home 侧比 home_handicap，away 侧比 away_handicap）。
    if market.side == SportsMarketSide.HOME:
        goalserve_handicap = gs.get("home_handicap")
    else:
        goalserve_handicap = gs.get("away_handicap")
    if not _line_matches(goalserve_handicap, market.line):
        return _reject(candidate, TailRejectReason.ODDS_GAP_LINE_MISMATCH.value)
    if not _spread_scope_matches(market, gs.get("market_name")):
        return _reject(candidate, TailRejectReason.ODDS_GAP_LINE_MISMATCH.value)

    home_implied = _to_decimal(gs.get("home_implied_prob"))
    away_implied = _to_decimal(gs.get("away_implied_prob"))
    if home_implied is None or away_implied is None:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)
    devig = _devig_two_way(home_implied, away_implied)
    if devig is None:
        return _reject(candidate, TailRejectReason.NO_ODDS_GAP.value)

    if market.side == SportsMarketSide.HOME:
        true_p = devig.home_true_p
    else:
        true_p = devig.away_true_p

    return _accept_odds_gap(candidate, policy, true_p, devig.overround)


def _totals_scope_matches(market: SportsMarketSnapshot, goalserve_market_name: Any) -> bool:
    """对账 totals 盘口范围：Polymarket 与 Goalserve 必须同为整场。

    Goalserve totals 抽取阶段已是整场（过滤了 half/quarter），仍二次确认盘口名；
    Polymarket 侧用 ``market_scope`` 判定，只接受 FULL_GAME。子周期 totals
    （篮球上半场、网球分盘/局数等）有各自的范围，与整场 Goalserve 赔率不可比。
    """

    if market_scope(market).scope_type != SportsMarketScopeType.FULL_GAME:
        return False
    return _goalserve_is_full_game_segment(goalserve_market_name)


def _spread_scope_matches(market: SportsMarketSnapshot, goalserve_market_name: Any) -> bool:
    """对账 spread 盘口范围：Polymarket 与 Goalserve 必须同为整场。

    spread 不走 ``totals_market_scope``，整场之外只可能是篮球上半场
    （BASKETBALL_FIRST_HALF）；只接受 FULL_GAME。
    """

    if market_scope(market).scope_type != SportsMarketScopeType.FULL_GAME:
        return False
    return _goalserve_is_full_game_segment(goalserve_market_name)


def _math_lock_veto(
    candidate: SportsTailCandidate,
    true_p: Decimal,
) -> tuple[Decimal, str | None]:
    """math_lock 一票否决 + true_p ceiling。

    Goalserve odds 可能 stale（pre-game 数据没在末段更新），odds_gap evaluator
    会算出 true_p=0.86 给已经数学上输掉的 token（实测 Kalinina first-set-winner
    第一盘已输但 Goalserve 仍显示 0.86 → 买 0.01 × 624 shares 全损 cost $6.24）。

    引入 math_lock 作为 entry-side 双重检查：
    - math_lock_prob 算出来 = 0 (lead<=0/已结束输方/没剩余时间已输) → veto，
      返回 (Decimal(0), 'math_lock_veto_lost')
    - math_lock_prob < 0.2 → 同 veto（数学上 80%+ 输方）
    - 否则用 math_lock_prob 作 true_p 上限：min(odds_true_p, math_lock_prob)，
      因为 math_lock 是公式硬概率，odds 可能误差大，取保守。

    返回 (effective_true_p, veto_reason or None)。
    """
    from strategies.sports_framework.math_lock import evaluate_math_lock

    market = candidate.market
    lock = evaluate_math_lock(
        market.market_type, market.side, market.line, candidate.game,
        market_slug=market.market_slug,
    )
    # math_lock unsupported → 放行（§17 不放过任何可盈利市场）。
    # 子段/特殊 prop / 球员 props 等没有专用公式的盘口仍可尝试用 Goalserve odds
    # 入场——edge 判定与 Kelly fraction 仍能起作用，不一刀切拒绝。
    if lock.method == "unsupported":
        return true_p, None
    # 只对"明确已输"才 veto（lock=0 + reason 含输方关键词）。
    # "side_not_leading"（当下未领先但仍可能赢）/ "not_leading_after_handicap"
    # （让分后未领先）/ "missing_*" 等不算输方，放行让 Kelly 用 Goalserve odds。
    veto_reasons = (
        "already_lost",
        "already_exceeded_line_lose",
        "already_over_lose",
        "match_already_lost",
        "set_already_lost",
        "no_remaining_half_innings",  # 已无剩余 + lock=0 = 输方
        "no_remaining_time",
        "no_remaining_balls_or_wickets",
        "chase_target_reached",  # 反方向已锁定
        "run_already_scored_in_first",  # NRFI 已输
        "halftime_settled",  # halftime 已决出输方
        "first_inning_completed",  # NRFI inning 1 已结束输方
        "quarter_ended",  # 该节已结束输方
        "match_ended",
        "game_ended",
        "game_already_ended",
    )
    if (
        lock.lock_probability <= Decimal("0.05")
        and any(kw in lock.reason for kw in veto_reasons)
    ):
        return Decimal("0"), f"math_lock_veto_lost:{lock.method}:{lock.reason}:lock={lock.lock_probability}"
    # math_lock 作 true_p 上限（取保守，但不强制 veto）
    if lock.lock_probability > Decimal("0") and lock.lock_probability < true_p:
        return lock.lock_probability, None
    return true_p, None


def _accept_odds_gap(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
    true_p: Decimal,
    overround: Decimal,
) -> TailEvaluation:
    """对已确定我方去抽水真实概率的候选产出 accept,把 true_p / edge_net 写 metadata。

    edge_gross = 去抽水真实概率 − Polymarket 买入 ask
    edge_net   = edge_gross − fee_per_share(taker fee 30 bps × price)

    设计:**无条件 accept**(只要 best_ask 存在)。Kelly 内部会基于 true_p / price /
    fee 自动算 fraction:net edge ≤ 0 时 Kelly reject,net edge > 0 时按比例下注。
    这是"无条件信任 Kelly"——不在 Kelly 之上叠 odds_gap_min_edge 这种 cap。
    moneyline/totals/spread 三类共用此尾段,保证 ODDS_GAP 机会语义完全一致。
    """

    market = candidate.market
    # best_ask 已由各分派函数确认非 None。
    assert market.best_ask is not None
    # math_lock 一票否决：Goalserve odds 可能 stale，math_lock 是公式硬概率，
    # 数学上已输（lock<=0.2）时即使 odds 显示高 true_p 也 veto。Kalinina case
    # 第一盘已输 lock=0 但 Goalserve odds 显示 0.86，差点全损。
    effective_true_p, veto_reason = _math_lock_veto(candidate, true_p)
    if veto_reason is not None:
        return _reject(
            candidate, TailRejectReason.NO_ODDS_GAP.value,
            metadata={"odds_gap_reject_reason": veto_reason,
                      "odds_gap_raw_true_p": str(true_p),
                      "odds_gap_math_lock_capped_p": str(effective_true_p)},
        )
    true_p = effective_true_p
    edge_gross = true_p - market.best_ask
    # Polymarket 当前 taker 默认 30 bps × price(see infra/polymarket fee schedule)。
    # 用 Decimal 避免浮点累积误差;_accept_odds_gap metadata 仅审计用,
    # 真正 Kelly 内部用 market.fee_rate_bps 重新算 net edge,这里不影响下注大小。
    fee_per_share = (market.best_ask * Decimal("30") / Decimal("10000")).quantize(Decimal("0.000001"))
    edge_net = edge_gross - fee_per_share
    # 实证 PRICE_NEGATIVE_EV_ZONE 守卫已撤回：违背"无条件信任 Kelly"哲学
    # （CLAUDE.md memory feedback_trust_kelly_no_extra_caps）。Kelly 自带 fraction
    # 收缩，edge 弱时 stake 自然小。/runtime/win-rate 显示的 0.40-0.60 区间
    # -$45 损失，后续通过 true_p 估算更保守 / Kelly 参数调小 fraction 而非硬拒。
    # 历史指标本身保留在 /runtime/win-rate 供操盘人工调参参考。

    enriched = SportsTailCandidate(
        game=candidate.game,
        market=candidate.market,
        reason="odds_gap_entry",
        risk_notes=candidate.risk_notes,
        metadata={
            **candidate.metadata,
            "odds_gap_true_p": str(true_p),
            "odds_gap_edge": str(edge_gross),
            "odds_gap_edge_net": str(edge_net),
            "odds_gap_fee_per_share": str(fee_per_share),
            "odds_gap_best_ask": str(market.best_ask),
            "odds_gap_overround": str(overround),
            "odds_gap_side": market.side.value,
            "odds_gap_market_type": market.market_type.value,
        },
    )
    return _accept(
        enriched,
        "odds_gap_entry",
        policy.odds_gap_execution_permission,
        opportunity_type=SportsTailOpportunityType.ODDS_GAP,
    )
