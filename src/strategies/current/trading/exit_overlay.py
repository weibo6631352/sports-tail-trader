"""体育扫尾入场后的退出 overlay：动态退出决策、一档 profit-take SELL 与资金占用效率门禁。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Mapping

from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.sports_live import BaseballGameState, TennisGameState, VolleyballGameState
from polymarket_trader.extension_api import ExtensionContext

from strategies.current.config import CurrentStrategyConfig
from strategies.current.exit_plan import cap_price_to_clob_limit
from strategies.current.outcomes import target_for_token
from strategies.current.tail import SportsMarketSide

# fair value clamp 边界：概率不会真正到 0/1，极端值会让止盈/止损判断失真。
_FAIR_VALUE_MIN = Decimal("0.01")
_FAIR_VALUE_MAX = Decimal("0.99")

# 盘口视为"已结算"的 best bid 门槛：bid 到 ~1.0 时几乎确定结算到 1，直接吃掉。
_SETTLED_BID = Decimal("0.99")

# 资金占用效率的 hold_hours 下限：避免比赛刚结束 / 估算极小时除零。
_MIN_HOLD_HOURS = Decimal("0.05")  # 3 分钟

# 每个持仓的 best bid 峰值跟踪：键 (condition_id, token_id) → 历史最高 best bid。
# 用于 riding-uptrend（创新高 → 不退）与 trailing-reversal（从峰值回撤 → 退）。
# 模块级状态：进程内跨决策周期累积；测试用 reset_dynamic_exit_peaks() 清空。
_dynamic_exit_peaks: dict[tuple[str, str], Decimal] = {}

# 每个持仓的双侧深度失衡比峰值跟踪：键 (condition_id, token_id) → 历史最高
# 失衡比。失衡比 = bid depth /（bid depth + ask depth），>0.5 买方占优。
# 当前失衡比从峰值向卖方倾斜显著下降 → 买方撤离 + 卖方堆单 → 风向逆转。
# 模块级状态：进程内跨决策周期累积；测试用 reset_dynamic_exit_peaks() 清空。
_dynamic_exit_imbalance_peaks: dict[tuple[str, str], Decimal] = {}


def reset_dynamic_exit_peaks() -> None:
    """清空 best bid 与失衡比峰值跟踪表。仅供测试隔离用——均为模块级累积状态。"""

    _dynamic_exit_peaks.clear()
    _dynamic_exit_imbalance_peaks.clear()


def previous_peak(key: tuple[str, str]) -> Decimal | None:
    """读取该持仓此前观察到的 best bid 峰值；从未观察过返回 None。"""

    return _dynamic_exit_peaks.get(key)


def observe_peak(key: tuple[str, str], bid: Decimal) -> Decimal:
    """记录一次 best bid 观察，返回更新后的峰值 = max(此前峰值, 本次 bid)。"""

    prev = _dynamic_exit_peaks.get(key)
    peak = bid if prev is None or bid > prev else prev
    _dynamic_exit_peaks[key] = peak
    return peak


def previous_imbalance_peak(key: tuple[str, str]) -> Decimal | None:
    """读取该持仓此前观察到的双侧深度失衡比峰值；从未观察过返回 None。"""

    return _dynamic_exit_imbalance_peaks.get(key)


def observe_imbalance_peak(key: tuple[str, str], imbalance: Decimal) -> Decimal:
    """记录一次失衡比观察，返回更新后的峰值 = max(此前峰值, 本次失衡比)。"""

    prev = _dynamic_exit_imbalance_peaks.get(key)
    peak = imbalance if prev is None or imbalance > prev else prev
    _dynamic_exit_imbalance_peaks[key] = peak
    return peak


def _depth_walked_exit(
    bids: tuple[PriceLevel, ...],
    shares: Decimal,
) -> tuple[Decimal | None, bool, Decimal]:
    """沿 bid 簿从最优档（最高价）逐档向下撮合，算出卖出 ``shares`` 的真实成交结果。

    返回 ``(clearing_price, fully_covered, realized_avg_price)``：
    - ``clearing_price``：吃完 ``shares`` 所触达的最深档位价（最差价）。在该价位
      挂 SELL 限价单会一路撮合掉我方全部份额。bid 簿为空时为 None。
    - ``fully_covered``：bid 簿累计深度是否足够吃完全部 ``shares``。
    - ``realized_avg_price``：已撮合份额上的 size 加权平均价——我方实际能拿到的均价。

    bid 簿不足以吃完时：clearing_price 取最深一档，realized_avg 仅对已覆盖部分
    加权（不虚构未撮合份额的价格）。
    """

    if not bids:
        return None, False, Decimal("0")

    remaining = shares
    notional = Decimal("0")
    filled = Decimal("0")
    clearing_price = bids[0].price
    for level in bids:
        clearing_price = level.price
        take = level.size if level.size <= remaining else remaining
        notional += take * level.price
        filled += take
        remaining -= take
        if remaining <= Decimal("0"):
            break

    fully_covered = remaining <= Decimal("0")
    realized_avg = notional / filled if filled > Decimal("0") else Decimal("0")
    return clearing_price, fully_covered, realized_avg


def _bid_depth_within_band(
    bids: tuple[PriceLevel, ...],
    best_bid: Decimal,
    band_fraction: Decimal,
) -> Decimal:
    """统计 best_bid 下方 ``band_fraction`` 价格区间内所有 bid 档位的 USDC 名义额。

    名义额 = Σ price × size，作为"买方力量"代理：区间内挂单越多，买方越厚。
    """

    floor_price = best_bid * (Decimal("1") - band_fraction)
    return sum(
        (level.price * level.size for level in bids if level.price >= floor_price),
        Decimal("0"),
    )


def _ask_depth_within_band(
    asks: tuple[PriceLevel, ...],
    best_ask: Decimal | None,
    band_fraction: Decimal,
) -> Decimal:
    """统计 best_ask 上方 ``band_fraction`` 价格区间内所有 ask 档位的 USDC 名义额。

    名义额 = Σ price × size，作为"卖方力量"代理：区间内挂单越多，卖方越厚。
    无 ask（best_ask 为 None）时返回 0——卖方力量为 0。
    """

    if best_ask is None:
        return Decimal("0")
    ceil_price = best_ask * (Decimal("1") + band_fraction)
    return sum(
        (level.price * level.size for level in asks if level.price <= ceil_price),
        Decimal("0"),
    )


@dataclass(frozen=True, slots=True)
class DynamicExitDecision:
    """动态退出评估结果：是否退出、退出挂卖价、可审计原因和审计 metadata。

    ``should_exit=False`` 表示本周期 HOLD（不挂 SELL）；``should_exit=True``
    时 ``exit_price`` 必为逐档撮合价 clearing_price——在该价挂 SELL 限价单
    会一路吃掉我方全部份额，调用侧据此定价退出 SELL。
    """

    should_exit: bool
    exit_price: Decimal | None
    reason: str
    metadata: dict[str, object]


def evaluate_dynamic_exit(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    token_id: str | None,
    entry_price: Decimal,
) -> DynamicExitDecision | None:
    """每个决策周期基于实时盘口深度 + 直播状态 + Goalserve 赔率动态重估退出。

    多因子动态退出决策引擎，决策顺序首个命中即返回（CLAUDE.md §17 买卖原则）。
    关键：定价不看 best_bid 单档，而看沿 bid 簿逐档撮合掉我方全部份额后的真实
    成交均价 realized_avg——薄簿里 best_bid 之下的份额会以更差价成交。

    1. 止损：fair value ≤ entry × stop_loss_fraction → 立即离场（紧急，吃滑点
       也要割）；exit_price = 逐档撮合价 clearing_price。永不在亏损上 trail。
    2. 水下 HOLD：best bid ≤ 买入价但未触止损 → HOLD，等待回到买入价之上。
    3. 已结算：best bid ≥ lock_in_price 且 ≥ 0.99（盘口几乎结算）→ 吃掉 bid。
    4. 顺势上行 HOLD：realized_avg 创新高 → 趋势仍向我方发展，骑住动量。
       这是核心规则，必须排在止盈 / 回撤判据之前。
    5. 止盈区判定：realized_avg ≥ entry × take_profit_multiple，或（Goalserve
       赔率公允价且 realized_avg ≥ fair value），或 realized_avg ≥ lock_in_price。
    6. 回撤反转：在止盈区且 realized_avg 从峰值回撤 ≥ retreat_fraction → 反转，
       在接近峰值处兑现。
    7. bid 深度坍缩：浮盈中且当前 bid depth 跌破峰值的 thinning_fraction →
       买方撤离，抢在 bid 进一步枯竭前兑现。
    8. 资金占用效率：持有到结算的每小时收益率 < min_hold_return_per_hour →
       剩余路程相对占用资金太慢，卖出腾资金重新部署。
    9. 否则 HOLD。

    滑点门禁：若 bid 簿吃不完全部份额，或 (best_bid-realized_avg)/best_bid 超过
    max_slippage，非紧急止盈分支（4/5/6/7/8 中的止盈）改为 HOLD 等深度回补，
    不把仓位砸进薄簿；止损（紧急）与已结算分支不受此约束。

    无盘口 / 无 best bid 时返回 None，让既有静态/结算退出行为继续生效——
    不破坏 no-orderbook 路径。退出挂卖价取逐档撮合价（每周期重新定价）。
    """

    orderbook = _exit_orderbook(context, token_id)
    if orderbook is None or orderbook.best_bid is None:
        return None  # 无实时盘口：交回静态/结算退出路径，不产出动态决策。
    # 关键：sell_actionable=False（FLOOR_BID_ONLY / DUST_BID）守门**只拦非紧急
    # 路径**（take_profit / HOLD / 回撤）——这些路径在地板 bid 上挂 SELL 没意义。
    # **但 stop_loss / settled 路径必须放行**：地板 bid 状态下不止损 = 等结算 =
    # 全损；挂 SELL @ floor_price (entry × stop_loss_floor_fraction) 不会被地板
    # 单秒吃（best_bid $0.01 < my ask $0.51 不成交），只等真买家出现。
    # 实测 chi-shp-jin spread $23.55 → $0.01 没止损就是因为顶层一刀切守门误杀。
    is_sell_actionable = orderbook.sell_actionable

    best_bid = orderbook.best_bid

    # 持仓份额：优先 position.shares，回退 size_shares——退出路径不同入口填不同字段。
    position_shares = _resolve_position_shares(context)

    # 逐档撮合：沿 bid 簿吃掉全部份额，得到真实退出价与成交均价。
    clearing_price, fully_covered, realized_avg = _depth_walked_exit(
        orderbook.bids, position_shares
    )
    if clearing_price is None:
        # bids 元组为空但 best_bid 存在：退回单档撮合假设，避免破坏 no-depth 路径。
        clearing_price = best_bid
        realized_avg = best_bid
        fully_covered = True

    # 滑点：薄簿里 best_bid 之下份额成交更差，realized_avg 低于 best_bid。
    slippage_fraction = Decimal("0")
    if best_bid > Decimal("0") and realized_avg < best_bid:
        slippage_fraction = (best_bid - realized_avg) / best_bid
    # 簿太薄判定：吃不完全部份额、滑点超容忍上限、或 sell_actionable=False
    # (FLOOR_BID_ONLY / DUST_BID)。任一命中 → take_profit 分支降级 HOLD 等深度，
    # 不在地板/dust bid 上挂 SELL 砸自己（B3）。stop_loss 不看 book_too_thin
    # 直接执行（地板状态下不止损 = 等结算全损，必须挂 SELL @ floor_price 等真买家）。
    book_too_thin = (
        not fully_covered
        or slippage_fraction > config.tail_dynamic_exit_max_slippage_fraction
        or not is_sell_actionable
    )

    fair_value, fair_value_source = _estimate_fair_value(
        context,
        token_id=token_id,
        best_bid=best_bid,
        best_ask=orderbook.best_ask,
    )

    # best bid 峰值跟踪改为跟踪 realized_avg：用真实可实现价判断"是否创新高"。
    # 严格大于（>）而非 >=：价位持平不算创新高——否则浮盈持仓在稳定价位上
    # 永远落入"riding_uptrend" HOLD 分支，永不进入 trailing_reversal / take_profit
    # 路径（实测 atp-nedic first-set-total 11232 shares cv +185% 没卖正是此 bug）。
    # 首次观察（prev_peak=None）仍走 HOLD 让 peak 初始化。
    condition_id = _resolve_condition_id(context, orderbook)
    peak_key = (condition_id or "", token_id or "")
    prev_peak = previous_peak(peak_key)
    is_new_high = prev_peak is None or realized_avg > prev_peak
    peak = observe_peak(peak_key, realized_avg)

    # 双侧深度跟踪：统计最优价上下带宽内 bid / ask 名义额，算失衡比并记录峰值。
    # 失衡比 = bid /（bid+ask），>0.5 买方占优、<0.5 卖方占优；双侧皆空时取
    # 0.5（中性，不产生信号）。
    band = config.tail_dynamic_exit_depth_band_fraction
    bid_depth = _bid_depth_within_band(orderbook.bids, best_bid, band)
    ask_depth = _ask_depth_within_band(orderbook.asks, orderbook.best_ask, band)
    total_depth = bid_depth + ask_depth
    imbalance = bid_depth / total_depth if total_depth > Decimal("0") else Decimal("0.5")
    prev_imbalance_peak = previous_imbalance_peak(peak_key)
    imbalance_peak = observe_imbalance_peak(peak_key, imbalance)

    stop_loss_threshold = entry_price * config.tail_dynamic_exit_stop_loss_fraction
    metadata: dict[str, object] = {
        "dynamic_exit_best_bid": str(best_bid),
        "dynamic_exit_clearing_price": str(clearing_price),
        "dynamic_exit_realized_avg": str(realized_avg),
        "dynamic_exit_fully_covered": fully_covered,
        "dynamic_exit_slippage_fraction": _decimal_metadata_text(slippage_fraction),
        "dynamic_exit_position_shares": str(position_shares),
        "dynamic_exit_bid_depth_usdc": _decimal_metadata_text(bid_depth),
        "dynamic_exit_ask_depth_usdc": _decimal_metadata_text(ask_depth),
        "dynamic_exit_depth_imbalance": _decimal_metadata_text(imbalance),
        "dynamic_exit_depth_imbalance_peak": _decimal_metadata_text(imbalance_peak),
        "dynamic_exit_fair_value": str(fair_value),
        "dynamic_exit_fair_value_source": fair_value_source,
        "dynamic_exit_entry_price": str(entry_price),
        "dynamic_exit_stop_loss_threshold": str(stop_loss_threshold),
        "dynamic_exit_peak_bid": str(peak),
        "dynamic_exit_is_new_high": is_new_high,
    }

    # 1. 多信号联立止损（用户设计：盘口优先 > 赔率 > math_lock，数据驱动）。
    #    流动性分层是基础信号：薄盘里 best_bid/imbalance 都不可信（一笔小单就翻转），
    #    必须按 bid 簿总 USDC 深度分档信任：
    #    - 极薄盘 (< $20)：bid 信号完全弃用，靠 math_lock 决策（HOLD 等结算 vs 主动放弃）
    #    - 薄盘 (< $100)：bid 信号 0.5 权重，floor 收紧 (entry×0.7 限亏 30%)
    #    - 健康盘 (≥ $100)：5 类信号完整投票，floor entry×0.6 (限亏 40%)
    math_locked = _is_math_locked_for_position(context)
    goalserve_implied = _goalserve_implied_prob_for_token(context, token_id)
    math_lock_prob = _math_lock_fair_value(context, token_id)
    bid_depth_usdc = sum(
        (level.price * level.size for level in orderbook.bids), Decimal("0")
    )
    # 流动性分层
    LIQUIDITY_VERY_THIN = Decimal("20")
    LIQUIDITY_THIN = Decimal("100")
    if bid_depth_usdc < LIQUIDITY_VERY_THIN:
        liquidity_tier = "very_thin"
        bid_signal_weight = Decimal("0")  # bid 信号完全不可信
        floor_fraction = Decimal("0.75")   # 极薄盘 floor 最紧 (限亏 25%)
    elif bid_depth_usdc < LIQUIDITY_THIN:
        liquidity_tier = "thin"
        bid_signal_weight = Decimal("0.5")
        floor_fraction = Decimal("0.7")    # 限亏 30%
    else:
        liquidity_tier = "healthy"
        bid_signal_weight = Decimal("1")
        floor_fraction = Decimal("0.6")    # 限亏 40%
    bullish_vote = Decimal("0")
    bearish_vote = Decimal("0")
    vote_reasons: list[str] = []
    # 信号 0：fair_value 跌幅（融合后）— 阈值收紧，反应快少亏。
    # 跌 30% 强信号（旧 50%）；跌 20% 弱信号（旧 30%）。盘口风向最敏感，
    # 早识别 + early exit 比等大幅亏损后 dump 强。
    if fair_value <= entry_price * Decimal("0.7"):
        bearish_vote += 2  # 跌穿 30% 强信号
        vote_reasons.append(f"fair_value_collapse:{fair_value:.3f}<=entry*0.7")
    elif fair_value <= entry_price * Decimal("0.8"):
        bearish_vote += 1  # 跌 20% 弱信号
        vote_reasons.append(f"fair_value_weak:{fair_value:.3f}<=entry*0.8")
    elif fair_value >= entry_price * Decimal("1.1"):
        bullish_vote += 1
        vote_reasons.append(f"fair_value_strong:{fair_value:.3f}>=entry*1.1")
    # **价格/概率信号**（主导投票）：math_lock + goalserve_implied + best_bid_dev
    # 这三个都是真实概率/价格，可直接归一化与 entry 比较。
    #
    # 信号 1：math_lock 相对入场价偏离 — 最硬的概率信号（公式硬算）。
    # 买价 0.20 + math 0.50 = +0.30 偏离 → 数学严重低估，veto+2 强 HOLD；
    # 买价 0.80 + math 0.30 = -0.50 偏离 → 数学已劣势，bearish。
    if math_lock_prob is not None:
        ml_deviation = math_lock_prob - entry_price
        if ml_deviation >= Decimal("0.15"):
            bullish_vote += 2  # veto 强度
            vote_reasons.append(f"math_deviation_strong_pos:{ml_deviation:+.3f}")
        elif ml_deviation >= Decimal("0"):
            bullish_vote += 1
            vote_reasons.append(f"math_deviation_pos:{ml_deviation:+.3f}")
        elif ml_deviation < -Decimal("0.15"):
            bearish_vote += 2  # 数学硬亏，权重提升
            vote_reasons.append(f"math_deviation_strong_neg:{ml_deviation:+.3f}")
    # 信号 2：Goalserve 赔率相对入场价偏离 — 博彩市场视角。
    if goalserve_implied is not None:
        gs_deviation = goalserve_implied - entry_price
        if gs_deviation < -Decimal("0.1"):
            bearish_vote += 1
            vote_reasons.append(f"goalserve_deviation_neg:{gs_deviation:+.3f}")
        elif gs_deviation > Decimal("0.05"):
            bullish_vote += 1
            vote_reasons.append(f"goalserve_deviation_pos:{gs_deviation:+.3f}")
    # 信号 3：best_bid 相对入场价偏离 — 当下立即可成交价。
    # 薄盘里 best_bid 一砸就到地板，bid_signal_weight 控制信任度。
    bid_deviation = best_bid - entry_price
    if bid_deviation < -Decimal("0.15"):
        bearish_vote += 1 * bid_signal_weight  # 价格信号权重 1（不 2）— 价格 vs 概率有差异
        vote_reasons.append(f"bid_deviation_strong_neg:{bid_deviation:+.3f}*w={bid_signal_weight}")
    elif bid_deviation < -Decimal("0.05"):
        bearish_vote += Decimal("0.5") * bid_signal_weight
        vote_reasons.append(f"bid_deviation_neg:{bid_deviation:+.3f}*w={bid_signal_weight}")
    elif bid_deviation >= Decimal("0.15"):
        bullish_vote += 1 * bid_signal_weight
        vote_reasons.append(f"bid_deviation_strong_pos:{bid_deviation:+.3f}*w={bid_signal_weight}")
    elif bid_deviation >= Decimal("0.05"):
        bullish_vote += Decimal("0.5") * bid_signal_weight
        vote_reasons.append(f"bid_deviation_pos:{bid_deviation:+.3f}*w={bid_signal_weight}")
    # **趋势 filter**（修饰，不主导）：单时点 imbalance 是供需方向 ratio，被 MM
    # 假墙骗的概率高（[[feedback_orderbook_direction_delta]]）。优先用 10s 窗口
    # OFI 方向信号（context.metadata['orderbook_direction']）：
    # - direction_score: 基于 microprice 价位移归一 [-1,+1]
    # - flow_imbalance: real_depth 消耗失衡（ask 被吃多 = 买压 → 正）
    # 综合 direction_label（majority vote of score/momentum/flow）= 真订单流方向，
    # 比单时点 ratio 抗噪。OFI 缺失时退回 imbalance ratio 作弱兜底。
    ob_direction = context.metadata.get("orderbook_direction") if context.metadata else None
    used_ofi = False
    if isinstance(ob_direction, dict):
        label = str(ob_direction.get("direction_label") or "")
        try:
            confidence = Decimal(str(ob_direction.get("confidence") or "0"))
        except (ArithmeticError, ValueError, TypeError):
            confidence = None
        # 只在样本充足（confidence >= 0.5）时信任 OFI 方向
        if confidence is not None and confidence >= Decimal("0.5"):
            used_ofi = True
            if bearish_vote > Decimal("0") and label == "no":
                bearish_vote += Decimal("0.5") * bid_signal_weight
                vote_reasons.append(
                    f"trend_confirm_bearish:ofi_label={label}*conf={confidence}*w={bid_signal_weight}"
                )
            elif bullish_vote > Decimal("0") and label == "yes":
                bullish_vote += Decimal("0.5") * bid_signal_weight
                vote_reasons.append(
                    f"trend_confirm_bullish:ofi_label={label}*conf={confidence}*w={bid_signal_weight}"
                )
    if not used_ofi:
        # OFI 缺失（窗口样本不足/数据 reader 未注入）→ 退回单时点 imbalance ratio
        # 作弱兜底；阈值收紧（0.30/0.70）减少假墙误判。
        if bearish_vote > Decimal("0") and imbalance < Decimal("0.30"):
            bearish_vote += Decimal("0.5") * bid_signal_weight
            vote_reasons.append(f"trend_confirm_bearish_fallback:imbalance={imbalance:.3f}*w={bid_signal_weight}")
        elif bullish_vote > Decimal("0") and imbalance > Decimal("0.70"):
            bullish_vote += Decimal("0.5") * bid_signal_weight
            vote_reasons.append(f"trend_confirm_bullish_fallback:imbalance={imbalance:.3f}*w={bid_signal_weight}")
    # fair_value 辅助门禁：跌 20% 即触发评估（旧 30%）。盘口风向反应快，
    # 早识别 fair_value 走弱 + orderbook 卖方一致 → 立即止损少亏 10%。
    fair_value_bearish = fair_value <= entry_price * Decimal("0.8")
    metadata.update({
        "dynamic_exit_bullish_vote": str(bullish_vote),
        "dynamic_exit_bearish_vote": str(bearish_vote),
        "dynamic_exit_vote_reasons": vote_reasons,
        "dynamic_exit_goalserve_implied": (
            str(goalserve_implied) if goalserve_implied is not None else None
        ),
        "dynamic_exit_math_lock_prob": (
            str(math_lock_prob) if math_lock_prob is not None else None
        ),
        "dynamic_exit_math_locked": math_locked,
        "dynamic_exit_liquidity_tier": liquidity_tier,
        "dynamic_exit_bid_signal_weight": str(bid_signal_weight),
        "dynamic_exit_floor_fraction": str(floor_fraction),
    })
    # 极薄盘 stop_loss：bid 信号完全弃用，只在 math_lock/goalserve 强 bearish 时触发。
    # 不砸 best_bid（薄盘砸单到地板），挂 fair × 0.95 让自然流动性接。
    # 注意：极薄盘只 short-circuit stop_loss 决策，take_profit 仍走后续分支。
    if (
        liquidity_tier == "very_thin"
        and bearish_vote >= 2
        and bullish_vote == 0
        and fair_value_bearish
    ):
        exit_price = max(
            fair_value * Decimal("0.95"),
            entry_price * floor_fraction,
        )
        return DynamicExitDecision(
            should_exit=True,
            exit_price=exit_price.quantize(Decimal("0.001")),
            reason="dynamic_exit_stop_loss_thin_book",
            metadata={
                **metadata,
                "dynamic_exit_decision": "stop_loss_thin_book",
                "dynamic_exit_exit_price_source": "fair_x_0.95_or_floor",
            },
        )
    # 薄盘 / 健康盘：完整投票 + 自适应 floor（极薄盘已上面处理）
    # 阈值 2 等价于"fair_value collapse 单独+2 已够"或"orderbook bearish+1 其他确认"。
    # math_lock>=0.5 (bullish+2) 自动 veto 因为 net bearish 不成立。
    if (
        liquidity_tier != "very_thin"
        and fair_value_bearish
        and bearish_vote >= 2
        and bearish_vote > bullish_vote
    ):
        exit_floor = entry_price * floor_fraction
        safe_exit_price = max(clearing_price, exit_floor)
        return DynamicExitDecision(
            should_exit=True,
            exit_price=safe_exit_price,
            reason="dynamic_exit_stop_loss",
            metadata={
                **metadata,
                "dynamic_exit_decision": "stop_loss",
                "dynamic_exit_floor_protected": safe_exit_price > clearing_price,
                "dynamic_exit_clearing_vs_floor": str(clearing_price) + "/" + str(exit_floor),
            },
        )
    if liquidity_tier != "very_thin" and fair_value_bearish:
        return DynamicExitDecision(
            should_exit=False,
            exit_price=None,
            reason="dynamic_exit_hold_insufficient_bearish_signals",
            metadata={
                **metadata,
                "dynamic_exit_decision": "hold_insufficient_bearish",
            },
        )

    # 2. 水下但未触止损：best bid ≤ 买入价 → HOLD，等价格回到买入价之上再考虑止盈。
    if best_bid <= entry_price:
        return DynamicExitDecision(
            should_exit=False,
            exit_price=None,
            reason="dynamic_exit_hold",
            metadata={**metadata, "dynamic_exit_decision": "hold", "dynamic_exit_trigger": "below_entry"},
        )

    # 3. 已结算：best bid ≥ lock_in_price 且 ≥ 0.99（盘口几乎结算到 1）→ 直接吃掉。
    #    近确定结算，不受滑点门禁约束。
    if best_bid >= config.tail_dynamic_exit_lock_in_price and best_bid >= _SETTLED_BID:
        return DynamicExitDecision(
            should_exit=True,
            exit_price=clearing_price,
            reason="dynamic_exit_take_profit",
            metadata={
                **metadata,
                "dynamic_exit_decision": "take_profit",
                "dynamic_exit_trigger": "settled",
            },
        )

    # 非紧急止盈分支共用的薄簿 HOLD：簿太薄时不砸单，等深度回补。
    def _await_depth_decision(intended_trigger: str) -> DynamicExitDecision:
        return DynamicExitDecision(
            should_exit=False,
            exit_price=None,
            reason="dynamic_exit_hold",
            metadata={
                **metadata,
                "dynamic_exit_decision": "hold",
                "dynamic_exit_trigger": "awaiting_bid_depth",
                "dynamic_exit_blocked_trigger": intended_trigger,
            },
        )

    # 4. 顺势上行：realized_avg 创新高 → 盘口仍向我方发展，骑住动量，不在涨势中离场。
    #    必须排在止盈 / 回撤判据之前——趋势未反转时不提前兑现。
    if is_new_high:
        return DynamicExitDecision(
            should_exit=False,
            exit_price=None,
            reason="dynamic_exit_hold",
            metadata={
                **metadata,
                "dynamic_exit_decision": "hold",
                "dynamic_exit_trigger": "riding_uptrend",
            },
        )

    # 5. 止盈区判定：以下任一满足即视为已进入可兑现的浮盈区间。
    #    用 realized_avg（真实可实现价）而非 best_bid 单档判定。
    take_profit_multiple_hit = (
        realized_avg >= entry_price * config.tail_dynamic_exit_take_profit_multiple
    )
    odds_fair_value_hit = (
        fair_value_source == "goalserve_implied_prob" and realized_avg >= fair_value
    )
    lock_in_hit = realized_avg >= config.tail_dynamic_exit_lock_in_price
    in_take_profit_zone = take_profit_multiple_hit or odds_fair_value_hit or lock_in_hit
    metadata["dynamic_exit_in_take_profit_zone"] = in_take_profit_zone

    # 6. 回撤反转：在止盈区且 realized_avg 从峰值回撤 ≥ retreat_fraction → 顺势趋势
    #    已反转，在接近峰值处兑现，不让浮盈继续吐回去。
    retreat = peak - realized_avg
    retreat_threshold = peak * config.tail_dynamic_exit_trailing_retreat_fraction
    if in_take_profit_zone and retreat >= retreat_threshold:
        if book_too_thin:
            return _await_depth_decision("trailing_reversal")
        return DynamicExitDecision(
            should_exit=True,
            exit_price=clearing_price,
            reason="dynamic_exit_take_profit",
            metadata={
                **metadata,
                "dynamic_exit_decision": "take_profit",
                "dynamic_exit_trigger": "trailing_reversal",
                "dynamic_exit_retreat": str(retreat),
            },
        )

    # 7. 双侧深度失衡反转：浮盈中且失衡比从峰值向卖方倾斜下降达 reversal_drop
    #    → 买方在撤、卖方在堆（风向逆转），抢在价格被砸下来前按逐档价兑现。
    in_profit = realized_avg > entry_price
    imbalance_reversal = (
        prev_imbalance_peak is not None
        and (imbalance_peak - imbalance) >= config.tail_dynamic_exit_imbalance_reversal_drop
    )
    if in_profit and imbalance_reversal:
        if book_too_thin:
            return _await_depth_decision("depth_imbalance_reversal")
        return DynamicExitDecision(
            should_exit=True,
            exit_price=clearing_price,
            reason="dynamic_exit_take_profit",
            metadata={
                **metadata,
                "dynamic_exit_decision": "take_profit",
                "dynamic_exit_trigger": "depth_imbalance_reversal",
            },
        )

    # 8. 资金占用效率：估算继续持有到结算的每小时收益率。剩余 bid→1.0 的收益
    #    被占用资金时长摊薄后若低于门槛，说明这笔钱卡在低效持仓里，卖出腾资金。
    hold_minutes = _estimated_settlement_hold_minutes(config, context)
    hold_hours = Decimal(hold_minutes) / Decimal("60")
    if hold_hours < _MIN_HOLD_HOURS:
        hold_hours = _MIN_HOLD_HOURS
    hold_return_per_hour = (Decimal("1") - best_bid) / best_bid / hold_hours
    metadata["dynamic_exit_hold_return_per_hour"] = _decimal_metadata_text(hold_return_per_hour)
    if hold_return_per_hour < config.tail_dynamic_exit_min_hold_return_per_hour:
        if book_too_thin:
            return _await_depth_decision("capital_efficiency")
        return DynamicExitDecision(
            should_exit=True,
            exit_price=clearing_price,
            reason="dynamic_exit_take_profit",
            metadata={
                **metadata,
                "dynamic_exit_decision": "take_profit",
                "dynamic_exit_trigger": "capital_efficiency",
            },
        )

    # 9. 既未触止损、未结算、未创新高、未回撤反转、深度未坍缩、资金效率达标 → HOLD。
    return DynamicExitDecision(
        should_exit=False,
        exit_price=None,
        reason="dynamic_exit_hold",
        metadata={**metadata, "dynamic_exit_decision": "hold", "dynamic_exit_trigger": "holding"},
    )


def _resolve_position_shares(context: ExtensionContext) -> Decimal:
    """解析当前退出仓位的份额，用于沿 bid 簿逐档撮合定价。

    优先 position.shares（权威持仓快照），回退 size_shares（reconcile 退出路径
    填的目标份额）。两者皆缺时回退 0——调用侧 _depth_walked_exit 会得到
    realized_avg=0 触发水下/止损分支，不会误判为可止盈。
    """

    position = context.position
    if position is not None and position.shares > Decimal("0"):
        return position.shares
    if context.size_shares is not None and context.size_shares > Decimal("0"):
        return context.size_shares
    return Decimal("0")


def _resolve_condition_id(
    context: ExtensionContext,
    orderbook: OrderbookSnapshot,
) -> str | None:
    """解析当前持仓的 condition_id，用于 best bid 峰值跟踪键。

    优先 market，其次 position，最后 orderbook——退出路径不同入口填充的字段不同。
    """

    if context.market is not None:
        return context.market.condition_id
    if context.position is not None:
        return context.position.condition_id
    return orderbook.condition_id


def _exit_orderbook(
    context: ExtensionContext,
    token_id: str | None,
) -> OrderbookSnapshot | None:
    """解析当前退出 token 对应的盘口快照。

    优先 context.orderbook（同 token 时），否则从 market_token_views 取——
    reconcile 退出路径只填 market_token_views，不直接填 context.orderbook。
    """

    orderbook = context.orderbook
    if orderbook is not None and (token_id is None or orderbook.token_id == token_id):
        return orderbook
    if token_id is not None:
        for view in context.market_token_views:
            if view.token_id == token_id and view.orderbook is not None:
                return view.orderbook
    return orderbook


def _estimate_fair_value(
    context: ExtensionContext,
    *,
    token_id: str | None,
    best_bid: Decimal,
    best_ask: Decimal | None,
) -> tuple[Decimal, str]:
    """融合所有信号估算我方方向结算到 1.0 的真实概率（fair value）。

    旧实现是 priority fallback chain（goalserve > math > mid），任一可用就 short-circuit，
    其他信号被丢。重写为 **max 融合**：

        fair_value = max(
            goalserve_implied × 0.95,   # 博彩公司带 vig 折扣
            math_lock_prob × 0.95,      # 数学模型简化补偿
            orderbook_microprice,        # 短期市场共识
        )

    取 max 因为我方 = 赢方持仓，fair 越高越接近真实结算（1.0）→ 保护利润不会被
    某一个 stale 信号砸低 SELL 价。三者都缺则退回 mid/bid。
    """

    # 第 1 层：真概率信号（goalserve + math_lock）max 融合。
    # 取 max 因为我方持仓 = 赢方，越高的真概率越接近实际结算 → 保护利润。
    truth_candidates: list[tuple[Decimal, str]] = []
    goalserve_prob = _goalserve_implied_prob_for_token(context, token_id)
    if goalserve_prob is not None:
        truth_candidates.append((goalserve_prob, "goalserve_implied_prob"))
    math_prob = _math_lock_fair_value(context, token_id)
    if math_prob is not None:
        truth_candidates.append((math_prob, "math_lock"))
    if truth_candidates:
        best_value, best_source = max(truth_candidates, key=lambda x: x[0])
        return _clamp_fair_value(best_value), best_source

    # 第 2 层：盘口信号（microprice/mid）— 真概率全缺时兜底。
    # 不与第 1 层 max，避免盘口 spread 大时 mid 拉高 fair_value 让 SELL 挂不出去。
    # 用 OrderbookSnapshot.microprice @property：处理 NO_BID/CEILING_ONLY/
    # NO_ASK/FLOOR_ONLY 边界返回 None，比纯算术更严谨。
    orderbook = context.orderbook
    if orderbook is not None:
        microprice = orderbook.microprice
        if microprice is not None:
            return _clamp_fair_value(microprice), "microprice"
    if best_ask is not None and best_ask > best_bid:
        mid = (best_bid + best_ask) / Decimal("2")
        return _clamp_fair_value(mid), "market_mid"

    # 第 3 层：仅 bid 兜底
    return _clamp_fair_value(best_bid), "best_bid"


def _math_lock_fair_value(
    context: ExtensionContext,
    token_id: str | None,
) -> Decimal | None:
    """从 math_lock 模型拿我方方向的 lock_probability 作 fair_value。

    覆盖三条路径（用户"所有盘口都必须有数学锁定"）：
    1. sport_framework.evaluate_math_lock：sport-specific (baseball/soccer/
       basketball/tennis/hockey/cricket) 公式
    2. series winner family：调 series_win_probability(state, p_per_game)
       用 best_of + games_won + 历史胜率算 best-of-N 系列赛 lock
    3. 都缺 → 返回 None 让上层 fallback goalserve
    """

    from strategies.sports_framework.math_lock import evaluate_math_lock
    from strategies.sports_framework.parsing import live_game_state_from_metadata
    from strategies.current.outcomes import describe_sports_market, target_for_token

    market = context.market
    if market is None or token_id is None:
        return None
    descriptor = describe_sports_market(market)
    if not descriptor.accepted or descriptor.market_type is None:
        return None
    target = target_for_token(market, token_id)
    if target is None:
        return None
    # 路径 2：series winner family 走专属模型
    series_prob = _series_winner_lock_prob(context, target)
    if series_prob is not None:
        return series_prob
    # 路径 1：单场 sport-specific
    game = live_game_state_from_metadata(context.metadata)
    if game is None:
        return None
    result = evaluate_math_lock(
        descriptor.market_type,
        target.side,
        descriptor.line,
        game,
        market_slug=market.market_slug,
    )
    if result.method == "unsupported" or result.lock_probability <= Decimal("0"):
        return None
    return result.lock_probability


def _series_winner_lock_prob(
    context: ExtensionContext,
    target: Any,
) -> Decimal | None:
    """系列赛胜者 lock：用 best_of + games_won + 历史单场胜率算 P(我方赢系列赛)。

    单场胜率 fallback = games_won / total_games_played (历史频率)；首场无历史
    用 0.5 中性。best_of-N 用负二项分布累加。
    """

    from strategies.current.series.types import SeriesState
    try:
        from strategies.current.series.winner_model import series_win_probability
    except ImportError:
        return None
    metadata = context.metadata or {}
    series_payload = metadata.get("series_state") or metadata.get("series")
    if not isinstance(series_payload, dict):
        return None
    best_of = series_payload.get("best_of")
    wins_home = series_payload.get("home_wins") or series_payload.get("wins_a")
    wins_away = series_payload.get("away_wins") or series_payload.get("wins_b")
    if best_of is None or wins_home is None or wins_away is None:
        return None
    try:
        best_of = int(best_of)
        wins_home = int(wins_home)
        wins_away = int(wins_away)
    except (TypeError, ValueError):
        return None
    if target.side == SportsMarketSide.HOME:
        my_wins, opp_wins = wins_home, wins_away
    elif target.side == SportsMarketSide.AWAY:
        my_wins, opp_wins = wins_away, wins_home
    else:
        return None
    state = SeriesState(best_of=best_of, wins_a=my_wins, wins_b=opp_wins)
    games_played = my_wins + opp_wins
    p_per_game = (
        Decimal(my_wins) / Decimal(games_played) if games_played > 0 else Decimal("0.5")
    )
    return series_win_probability(state, p_per_game)


def _clamp_fair_value(value: Decimal) -> Decimal:
    """把 fair value 收敛到 [0.01, 0.99]，避免极端概率扭曲止盈/止损判断。"""

    return min(_FAIR_VALUE_MAX, max(_FAIR_VALUE_MIN, value))


def _goalserve_implied_prob_for_token(
    context: ExtensionContext,
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


def _capital_efficiency_gate(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    entry_price: Decimal,
    amount_usdc: Decimal,
    tail_metadata: Mapping[str, object],
) -> tuple[bool, str, dict[str, object]]:
    """评估体育扫尾入场的预期利润和资金占用效率。

    结算收益足够时允许持有到权威结算，同时如果一档 profit-take SELL 已有
    足够毛利润，也标记买入成交后挂单释放资金；结算效率不足时，只允许这些
    能挂出达标 profit-take SELL 的订单继续进入主链路。
    """

    if "tail_reason" not in tail_metadata:
        return True, "", {}
    if entry_price <= Decimal("0") or entry_price >= Decimal("1"):
        return False, "profit_take_not_viable", {
            "exit_mode": "blocked",
            "capital_efficiency_reason": "entry_price_not_profitable",
        }

    shares = amount_usdc / entry_price
    expected_settlement_profit = shares * (Decimal("1") - entry_price)
    hold_minutes = _estimated_settlement_hold_minutes(config, context)
    expected_profit_per_hour = expected_settlement_profit * Decimal("60") / Decimal(hold_minutes)
    metadata: dict[str, object] = {
        "expected_settlement_profit_usdc": _decimal_metadata_text(expected_settlement_profit),
        "expected_settlement_profit_per_hour_usdc": _decimal_metadata_text(
            expected_profit_per_hour
        ),
        "estimated_settlement_hold_minutes": hold_minutes,
        "min_expected_profit_usdc": str(config.tail_min_expected_profit_usdc),
        "min_expected_profit_per_hour_usdc": str(config.tail_min_expected_profit_per_hour_usdc),
    }
    settlement_efficient = (
        expected_settlement_profit >= config.tail_min_expected_profit_usdc
        and expected_profit_per_hour >= config.tail_min_expected_profit_per_hour_usdc
    )
    if settlement_efficient:
        metadata["exit_mode"] = "settlement"
        profit_take_metadata = _profit_take_metadata(
            config,
            context,
            entry_price=entry_price,
            shares=shares,
        )
        if profit_take_metadata is not None:
            metadata.update(profit_take_metadata)
            metadata["profit_take_overlay_enabled"] = True
        return True, "", metadata

    profit_take_metadata = _profit_take_metadata(
        config,
        context,
        entry_price=entry_price,
        shares=shares,
    )
    if profit_take_metadata is None:
        metadata.update(
            {
                "exit_mode": "blocked",
                "capital_efficiency_reason": "profit_take_target_above_one",
            }
        )
        return False, "profit_take_not_viable", metadata

    metadata.update(profit_take_metadata)
    metadata["exit_mode"] = "profit_take"
    metadata["capital_efficiency_reason"] = "settlement_efficiency_below_min"
    profit_take_profit = Decimal(str(profit_take_metadata["profit_take_expected_profit_usdc"]))
    profit_take_profit_per_hour = Decimal(
        str(profit_take_metadata["profit_take_expected_profit_per_hour_usdc"])
    )
    if profit_take_profit < config.tail_profit_take_min_profit_usdc and (
        profit_take_profit_per_hour < config.tail_min_expected_profit_per_hour_usdc
    ):
        return False, "profit_take_not_viable", metadata
    if profit_take_profit < config.tail_profit_take_min_profit_usdc:
        metadata["capital_efficiency_reason"] = "profit_take_hourly_efficiency_high"
    return True, "", metadata


def _estimated_settlement_hold_minutes(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> int:
    """单场比赛等待结算的资金占用时间估算（分钟）。

    估算优先级：
    1. 单场比赛已结束（ENDED）且系列赛未完结：用系列赛剩余场次估算结算时间。
       封盘时间 ≠ 结算时间：Polymarket 对属于系列赛的市场可能等到系列完结才结算。
    2. 单场比赛已结束（ENDED）且无系列赛上下文：仅等待 Polymarket 权威结算缓冲。
    3. 单场比赛进行中、有时钟（篮球/足球/冰球）：seconds_remaining + 缓冲。
    4. 无时钟运动（棒球/网球）进行中：基于赛况状态推算剩余时间 + 缓冲。
    5. 无比赛数据：tail_settlement_hold_minutes 保守值。

    仅供 SINGLE_GAME 路径调用。Series/Outright 走独立 decide 函数
    （decide_series_entry / decide_outright_entry），有各自的风控预算模型，
    不经过 _capital_efficiency_gate。

    Polymarket end_date 对体育单场市场 = game_start_time，与封盘时间无关；
    封盘由 Polymarket 在赛事结果确认后自行决定。
    """
    from strategies.sports_framework import LiveGameStatus
    from strategies.sports_framework.parsing import live_game_state_from_metadata

    game = live_game_state_from_metadata(context.metadata)
    buffer = max(config.tail_settlement_buffer_minutes, 1)

    if game is not None:
        if game.status == LiveGameStatus.ENDED:
            # 如果 metadata 有系列赛热态且系列赛尚未完结，结算时间取决于系列完结
            series_hold = _series_settlement_hold_minutes_if_active(config, context)
            if series_hold is not None:
                return series_hold
            return buffer
        if game.status == LiveGameStatus.LIVE:
            if game.seconds_remaining is not None:
                return max(math.ceil(game.seconds_remaining / 60) + buffer, 1)
            estimated = _estimate_seconds_remaining_from_state(game)
            if estimated is not None:
                return max(math.ceil(estimated / 60) + buffer, 1)

    return max(int(config.tail_settlement_hold_minutes), 1)


def _series_settlement_hold_minutes_if_active(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> int | None:
    """若 metadata 含系列赛热态且系列赛未完结，返回估算到结算的分钟数；否则返回 None。

    适用于 SINGLE_GAME 市场：单场已结束但整个系列赛仍在进行时，Polymarket
    可能等到系列完结才批量结算所有相关市场，用封盘时间（比赛结束）会严重低估
    资金占用时间。
    """
    from strategies.current.series.match import series_state_from_metadata
    from strategies.current.series.winner_model import expected_games_remaining

    state = series_state_from_metadata(context.metadata or {})
    if state is None:
        return None

    needed_a = max(0, (state.best_of + 1) // 2 - state.wins_a)
    needed_b = max(0, (state.best_of + 1) // 2 - state.wins_b)
    if needed_a <= 0 or needed_b <= 0:
        return None  # 系列赛已有胜者，按常规缓冲结算

    exp_games = expected_games_remaining(state, Decimal("0.5"))
    avg_days = config.tail_series_avg_days_per_game
    buffer = max(config.tail_settlement_buffer_minutes, 1)
    now = context.now
    if now is None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

    next_game_at = state.next_game_at
    if next_game_at is not None:
        tz_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        tz_next = next_game_at if next_game_at.tzinfo else next_game_at.replace(tzinfo=timezone.utc)
        from datetime import timezone
        if tz_next > tz_now:
            days_to_next = (tz_next - tz_now).total_seconds() / 86400.0
            remaining_after_first = max(0.0, exp_games - 1.0)
            total_days = days_to_next + remaining_after_first * avg_days
            return max(int(total_days * 24 * 60) + buffer, buffer + 1)

    total_days = exp_games * avg_days
    return max(int(total_days * 24 * 60) + buffer, buffer + 1)


def _estimate_seconds_remaining_from_state(game: object) -> int | None:
    """为无游戏时钟的运动（棒球、网球、排球）从赛况状态推算剩余秒数。"""
    from strategies.sports_framework.types import LiveGameState

    if not isinstance(game, LiveGameState):
        return None
    if game.baseball_state is not None:
        return _baseball_seconds_remaining(game.baseball_state)
    if game.tennis_state is not None:
        return _tennis_seconds_remaining(game.tennis_state)
    if game.volleyball_state is not None:
        return _volleyball_seconds_remaining(game.volleyball_state)
    return None


def _is_math_locked_for_position(context: ExtensionContext) -> bool:
    """判断当前持仓是否处于"数学锁定" — 即剩余比赛得分窗口已无法让我方输。

    例: MLB B9th + Under 5.5 + 总分=5 → 即使 Dbacks B9th 得 1 分,总分=6 > 5.5 输,
    但若总分已 ≥ 5.5 即输,无 lock。**lock 条件**:
    - MLB Totals Under: 总分 < line 且剩余 ≤ 1 half-inning + 当前 inning 已结束 → lock
    - MLB Totals Over: 总分 ≥ line → 已锁,但市场早已结算无需 hold
    - MLB ML: 比分差距 > 剩余半局可能反转幅度 → lock(简化:不做,投出概率 hold)

    保守起见,只对 **Totals Under + MLB Bottom 9th** 这种最常见场景做 lock 守卫;
    其他 case 返回 False(止损路径维持原样)。
    """

    from strategies.sports_framework import LiveGameStatus
    from strategies.sports_framework.parsing import live_game_state_from_metadata
    from strategies.sports_framework.types import (
        SportsMarketSide,
        SportsMarketType,
    )
    from strategies.current.outcomes import describe_sports_market

    market = context.market
    if market is None:
        return False
    descriptor = describe_sports_market(market)
    if not descriptor.accepted or descriptor.market_type is None:
        return False
    # 当前 token 对应的 side(Under / Over / Home / Away)
    target = target_for_token(market, context.token_id or "")
    if target is None:
        return False

    game = live_game_state_from_metadata(context.metadata)
    if game is None or game.status != LiveGameStatus.LIVE:
        return False

    # 仅 MLB Totals Under 这一明确锁定场景。
    if descriptor.market_type != SportsMarketType.TOTALS:
        return False
    if target.side != SportsMarketSide.UNDER:
        return False
    if descriptor.line is None:
        return False
    if game.baseball_state is None or game.baseball_state.current_inning is None:
        return False

    state = game.baseball_state
    inning = state.current_inning
    half = (state.inning_half or "top").lower()
    # 当前总分
    home_score = game.home.score if game.home else None
    away_score = game.away.score if game.away else None
    if home_score is None or away_score is None:
        return False
    total_score = Decimal(int(home_score) + int(away_score))
    line = descriptor.line

    # 已经达到/超过 line → Under 注定输,不该 hold(让止损正常生效抢回部分本金)
    if total_score >= line:
        return False

    # 进入 Bottom 9th 且还在 Bottom 9th: 剩余 ≤ 1 half-inning,Dbacks 主场最后一击。
    # 即使 Dbacks 击出 (line - total_score) 分,total 才达 line,Under 输;若不足则赢。
    # 当 剩余得分窗口期望 < (line - total_score) 时 lock。MLB 单半局期望 ~1.0 分,
    # 但**实际单半局 ≥ N 分的概率随 N 急剧下降**: P(half-inning ≥ 2)≈0.18,≥ 3≈0.07,
    # ≥ 4≈0.025。差距 ≥ 2 分时 Under 胜率 ≥ 82%,可视为数学锁定守卫。
    if inning >= 9 and half == "bottom":
        # B9th Dbacks 主场,差距 ≥ 2 分时 hold(Under 大概率赢)
        gap = line - total_score
        if gap >= Decimal("2"):
            return True

    # 第 10+ 局加时 + bottom: 客队领先(score 差距 ≥ 1 已赢)、平局 + Under 差距充足 → hold
    if inning >= 10 and half == "bottom":
        gap = line - total_score
        if gap >= Decimal("1.5"):
            return True

    return False


# 棒球平均每半局约 10 分钟，标准比赛 9 局
_BASEBALL_MINUTES_PER_HALF_INNING = 10
_BASEBALL_REGULATION_INNINGS = 9


def _baseball_seconds_remaining(state: BaseballGameState) -> int | None:
    """基于当前局数和上下半局估算棒球比赛剩余秒数。"""
    if state.current_inning is None:
        return None
    inning = state.current_inning
    if inning >= _BASEBALL_REGULATION_INNINGS:
        # 第9局或加时：剩余半局极少
        remaining_half_innings = 1 if (state.inning_half or "top") == "bottom" else 2
    else:
        after = _BASEBALL_REGULATION_INNINGS - inning
        if (state.inning_half or "top") == "bottom":
            remaining_half_innings = 1 + after * 2
        else:
            remaining_half_innings = 2 + after * 2
    return remaining_half_innings * _BASEBALL_MINUTES_PER_HALF_INNING * 60


def _tennis_seconds_remaining(state: TennisGameState) -> int | None:
    """基于盘分和局分粗略估算网球比赛剩余秒数（默认三盘两胜制）。"""
    if state.current_set is None:
        return None
    sets_to_win = 2  # best-of-3 assumption; five-set Grand Slams will underestimate hold time ~40%
    sets_remaining_home = max(0, sets_to_win - state.home_sets_won)
    sets_remaining_away = max(0, sets_to_win - state.away_sets_won)
    avg_sets_remaining = (sets_remaining_home + sets_remaining_away) / 2.0
    current_games_remaining = 0
    if state.home_current_set_games is not None and state.away_current_set_games is not None:
        current_games_remaining = max(
            0, 6 - max(state.home_current_set_games, state.away_current_set_games)
        )
    estimated = int((avg_sets_remaining * 45 + current_games_remaining * 5) * 60)
    return max(estimated, 60)


# 排球平均每盘约 22 分钟（标准盘 25 分），决胜盘（第5盘）约 15 分钟
_VOLLEYBALL_MINUTES_PER_SET = 22


def _volleyball_seconds_remaining(state: VolleyballGameState) -> int | None:
    """基于当前盘分估算排球比赛剩余秒数（默认五盘三胜制）。"""
    if state.current_set is None:
        return None
    sets_to_win = 3  # best-of-5 standard format
    sets_remaining_home = max(0, sets_to_win - state.home_sets_won)
    sets_remaining_away = max(0, sets_to_win - state.away_sets_won)
    avg_sets_remaining = (sets_remaining_home + sets_remaining_away) / 2.0
    estimated = int(avg_sets_remaining * _VOLLEYBALL_MINUTES_PER_SET * 60)
    return max(estimated, 60)


def _profit_take_metadata(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    entry_price: Decimal,
    shares: Decimal,
) -> dict[str, object] | None:
    """计算一档 profit-take SELL 的审计 metadata。"""

    target_price = _profit_take_target_price(
        context,
        entry_price,
        offset=config.tail_profit_take_offset,
        multiplier=config.tail_profit_take_multiplier,
    )
    if target_price is None or target_price > Decimal("1"):
        return None
    expected_profit_take_profit = shares * (target_price - entry_price)
    hold_minutes = _estimated_profit_take_fill_minutes(config, context)
    expected_profit_take_profit_per_hour = expected_profit_take_profit * Decimal("60") / Decimal(
        hold_minutes
    )
    return {
        "profit_take_target_price": str(target_price),
        "profit_take_expected_profit_usdc": _decimal_metadata_text(
            expected_profit_take_profit
        ),
        "profit_take_expected_profit_per_hour_usdc": _decimal_metadata_text(
            expected_profit_take_profit_per_hour
        ),
        "profit_take_estimated_hold_minutes": hold_minutes,
        "profit_take_min_profit_usdc": str(config.tail_profit_take_min_profit_usdc),
    }


def _bid_depth_usdc(context: ExtensionContext) -> Decimal | None:
    """计算盘口 bid 侧总深度（USDC），作为市场活跃度代理。"""
    ob = context.orderbook
    if ob is None:
        return None
    if ob.bids:
        return sum((level.price * level.size for level in ob.bids), Decimal("0"))
    if ob.best_bid is not None and ob.best_bid_size is not None:
        return ob.best_bid * ob.best_bid_size
    return None


def _estimated_profit_take_fill_minutes(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> int:
    """按 bid 侧深度估算止盈 GTC SELL 的预期成交时间。

    流动性好（bid depth ≥ 阈值）→ 快速成交，用 tail_profit_take_hold_minutes。
    流动性差（bid depth < 阈值）→ 止盈单大概率等结算，用结算持仓时间估算。
    无盘口数据时退回 tail_profit_take_hold_minutes 默认值。
    """
    bid_depth = _bid_depth_usdc(context)
    if bid_depth is None or bid_depth >= config.tail_profit_take_liquid_bid_depth_usdc:
        return max(int(config.tail_profit_take_hold_minutes), 1)
    # 薄市场：挂单成交时间接近结算，用结算持仓时间估算
    return _estimated_settlement_hold_minutes(config, context)


def _profit_take_target_price(
    context: ExtensionContext,
    entry_price: Decimal,
    *,
    offset: Decimal | None = None,
    multiplier: Decimal | None = None,
) -> Decimal | None:
    """计算 profit-take 目标价。优先级 offset > multiplier > 上一档 tick。

    offset 不为 None 时：target = entry_price + offset，超 CLOB 上限收敛到 cap；
    固定 offset 让盘中提前止盈可达（买 0.88 → 卖 0.95），不必死等结算。
    multiplier 不为 None 时：target = entry_price × multiplier，超上限收敛到 cap。
    两者皆 None：target = entry_price + 1 tick（原资金效率模式）。
    """

    tick_size = _effective_tick_size(context)
    if tick_size is None or tick_size <= Decimal("0"):
        tick_size = Decimal("0.01")
    if offset is not None and offset > Decimal("0"):
        raw = entry_price + offset
        return cap_price_to_clob_limit(raw, tick_size=tick_size)
    if multiplier is not None and multiplier > Decimal("1"):
        raw = entry_price * multiplier
        return cap_price_to_clob_limit(raw, tick_size=tick_size)
    units = (entry_price / tick_size).to_integral_value(rounding=ROUND_FLOOR)
    target = (units + 1) * tick_size
    if target <= entry_price:
        target += tick_size
    return cap_price_to_clob_limit(target, tick_size=tick_size)


def _effective_tick_size(context: ExtensionContext) -> Decimal | None:
    """读取当前 market 或 orderbook 的最小价格跳动。"""

    if context.orderbook is not None and context.orderbook.tick_size is not None:
        return context.orderbook.tick_size
    if context.market is not None:
        return context.market.tick_size
    return None


def _decimal_metadata_text(value: Decimal) -> str:
    """把审计金额压到稳定小数位，避免无限循环小数撑大 metadata。"""

    return str(value.quantize(Decimal("0.000000000000000001")))


def _apply_profit_take_exit_plan(metadata: dict[str, object]) -> None:
    """把需要主动止盈的订单退出计划改写为买入后挂 profit-take SELL。"""

    if not _has_profit_take_follow_up(metadata):
        return
    target_price = metadata.get("profit_take_target_price")
    metadata["exit_target_price"] = target_price
    plan = metadata.get("exit_plan")
    if not isinstance(plan, dict):
        return
    plan["target_exit_price"] = target_price
    plan["primary_action"] = "place_profit_take_gtc_sell_after_buy_fill"
    plan["settlement_rule"] = "keep_profit_take_order_until_fill_or_authoritative_resolution"
    plan["recovery_rule"] = "cancel_open_entry_orders_and_cover_profit_take_positions"


def _has_profit_take_follow_up(metadata: Mapping[str, object]) -> bool:
    """判断 BUY 成交后是否需要立刻挂一档 profit-take SELL。"""

    return metadata.get("exit_mode") == "profit_take" or bool(
        metadata.get("profit_take_overlay_enabled")
    )
