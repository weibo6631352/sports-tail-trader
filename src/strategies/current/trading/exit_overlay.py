"""体育扫尾入场后的退出 overlay：动态退出决策、一档 profit-take SELL 与资金占用效率门禁。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from polymarket_trader.domain.orderbook import OrderbookSnapshot
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


def reset_dynamic_exit_peaks() -> None:
    """清空 best bid 峰值跟踪表。仅供测试隔离用——峰值是模块级累积状态。"""

    _dynamic_exit_peaks.clear()


def previous_peak(key: tuple[str, str]) -> Decimal | None:
    """读取该持仓此前观察到的 best bid 峰值；从未观察过返回 None。"""

    return _dynamic_exit_peaks.get(key)


def observe_peak(key: tuple[str, str], bid: Decimal) -> Decimal:
    """记录一次 best bid 观察，返回更新后的峰值 = max(此前峰值, 本次 bid)。"""

    prev = _dynamic_exit_peaks.get(key)
    peak = bid if prev is None or bid > prev else prev
    _dynamic_exit_peaks[key] = peak
    return peak


@dataclass(frozen=True, slots=True)
class DynamicExitDecision:
    """动态退出评估结果：是否退出、退出挂卖价、可审计原因和审计 metadata。

    ``should_exit=False`` 表示本周期 HOLD（不挂 SELL）；``should_exit=True``
    时 ``exit_price`` 必为当前 best bid，调用侧据此重新定价退出 SELL。
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
    """每个决策周期基于实时盘口 + 直播状态 + Goalserve 赔率动态重估退出。

    多因子动态退出决策引擎，决策顺序首个命中即返回（CLAUDE.md §17 买卖原则）：
    1. 止损：fair value ≤ entry × stop_loss_fraction → 立即离场，永不在亏损上 trail。
    2. 水下 HOLD：best bid ≤ 买入价但未触止损 → HOLD，等待回到买入价之上。
    3. 已结算：best bid ≥ lock_in_price 且 ≥ 0.99（盘口几乎结算）→ 吃掉 bid。
    4. 顺势上行 HOLD：best bid 创新高 → 趋势仍向我方发展，不退，骑住动量。
       这是核心规则，必须排在止盈 / 回撤判据之前。
    5. 止盈区判定：bid ≥ entry × take_profit_multiple，或（Goalserve 赔率公允价
       且 bid ≥ fair value），或 bid ≥ lock_in_price。
    6. 回撤反转：在止盈区且 best bid 从峰值回撤 ≥ retreat_fraction → 趋势反转，
       在接近峰值处兑现。
    7. 资金占用效率：持有到结算的每小时收益率 < min_hold_return_per_hour →
       剩余 bid→1.0 的路程相对占用资金太慢，卖出腾资金重新部署。
    8. 否则 HOLD。

    无盘口 / 无 best bid 时返回 None，让既有静态/结算退出行为继续生效——
    不破坏 no-orderbook 路径。退出挂卖价始终取当前 best bid（每周期重新定价）。
    """

    orderbook = _exit_orderbook(context, token_id)
    if orderbook is None or orderbook.best_bid is None:
        return None  # 无实时盘口：交回静态/结算退出路径，不产出动态决策。

    best_bid = orderbook.best_bid
    fair_value, fair_value_source = _estimate_fair_value(
        context,
        token_id=token_id,
        best_bid=best_bid,
        best_ask=orderbook.best_ask,
    )

    # 峰值跟踪：先读此前峰值再观察，据此判断 best bid 是否创新高。
    condition_id = _resolve_condition_id(context, orderbook)
    peak_key = (condition_id or "", token_id or "")
    prev_peak = previous_peak(peak_key)
    is_new_high = prev_peak is None or best_bid >= prev_peak
    peak = observe_peak(peak_key, best_bid)

    stop_loss_threshold = entry_price * config.tail_dynamic_exit_stop_loss_fraction
    metadata: dict[str, object] = {
        "dynamic_exit_best_bid": str(best_bid),
        "dynamic_exit_fair_value": str(fair_value),
        "dynamic_exit_fair_value_source": fair_value_source,
        "dynamic_exit_entry_price": str(entry_price),
        "dynamic_exit_stop_loss_threshold": str(stop_loss_threshold),
        "dynamic_exit_peak_bid": str(peak),
        "dynamic_exit_is_new_high": is_new_high,
    }

    # 1. 止损：公允价值跌破买入价 × stop_loss_fraction → 比赛/赔率逆转，立即离场。
    #    亏损方向永不 trail——继续等只会输掉更多本金。
    if fair_value <= stop_loss_threshold:
        return DynamicExitDecision(
            should_exit=True,
            exit_price=best_bid,
            reason="dynamic_exit_stop_loss",
            metadata={**metadata, "dynamic_exit_decision": "stop_loss"},
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
    if best_bid >= config.tail_dynamic_exit_lock_in_price and best_bid >= _SETTLED_BID:
        return DynamicExitDecision(
            should_exit=True,
            exit_price=best_bid,
            reason="dynamic_exit_take_profit",
            metadata={
                **metadata,
                "dynamic_exit_decision": "take_profit",
                "dynamic_exit_trigger": "settled",
            },
        )

    # 4. 顺势上行：best bid 创新高 → 盘口仍向我方发展，骑住动量，不在涨势中离场。
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
    take_profit_multiple_hit = (
        best_bid >= entry_price * config.tail_dynamic_exit_take_profit_multiple
    )
    odds_fair_value_hit = (
        fair_value_source == "goalserve_implied_prob" and best_bid >= fair_value
    )
    lock_in_hit = best_bid >= config.tail_dynamic_exit_lock_in_price
    in_take_profit_zone = take_profit_multiple_hit or odds_fair_value_hit or lock_in_hit
    metadata["dynamic_exit_in_take_profit_zone"] = in_take_profit_zone

    # 6. 回撤反转：在止盈区且 best bid 从峰值回撤 ≥ retreat_fraction → 顺势趋势
    #    已反转，在接近峰值处兑现，不让浮盈继续吐回去。
    retreat = peak - best_bid
    retreat_threshold = peak * config.tail_dynamic_exit_trailing_retreat_fraction
    if in_take_profit_zone and retreat >= retreat_threshold:
        return DynamicExitDecision(
            should_exit=True,
            exit_price=best_bid,
            reason="dynamic_exit_take_profit",
            metadata={
                **metadata,
                "dynamic_exit_decision": "take_profit",
                "dynamic_exit_trigger": "trailing_reversal",
                "dynamic_exit_retreat": str(retreat),
            },
        )

    # 7. 资金占用效率：估算继续持有到结算的每小时收益率。剩余 bid→1.0 的收益
    #    被占用资金时长摊薄后若低于门槛，说明这笔钱卡在低效持仓里，卖出腾资金。
    hold_minutes = _estimated_settlement_hold_minutes(config, context)
    hold_hours = Decimal(hold_minutes) / Decimal("60")
    if hold_hours < _MIN_HOLD_HOURS:
        hold_hours = _MIN_HOLD_HOURS
    hold_return_per_hour = (Decimal("1") - best_bid) / best_bid / hold_hours
    metadata["dynamic_exit_hold_return_per_hour"] = _decimal_metadata_text(hold_return_per_hour)
    if hold_return_per_hour < config.tail_dynamic_exit_min_hold_return_per_hour:
        return DynamicExitDecision(
            should_exit=True,
            exit_price=best_bid,
            reason="dynamic_exit_take_profit",
            metadata={
                **metadata,
                "dynamic_exit_decision": "take_profit",
                "dynamic_exit_trigger": "capital_efficiency",
            },
        )

    # 8. 既未触止损、未结算、未创新高、未回撤反转、资金效率达标 → HOLD。
    return DynamicExitDecision(
        should_exit=False,
        exit_price=None,
        reason="dynamic_exit_hold",
        metadata={**metadata, "dynamic_exit_decision": "hold", "dynamic_exit_trigger": "holding"},
    )


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
    """估算我方方向结算到 1.0 的概率（fair value），并返回来源标签。

    优先级：Goalserve 盘口（moneyline/totals/spread）对我方方向的隐含概率 >
    Polymarket 市场中价。结果 clamp 到 [0.01, 0.99]。
    """

    goalserve_prob = _goalserve_implied_prob_for_token(context, token_id)
    if goalserve_prob is not None:
        return _clamp_fair_value(goalserve_prob), "goalserve_implied_prob"
    if best_ask is not None:
        mid = (best_bid + best_ask) / Decimal("2")
        return _clamp_fair_value(mid), "market_mid"
    # 无 best_ask：退回 best bid 作为保守 fair value 代理。
    return _clamp_fair_value(best_bid), "best_bid"


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
