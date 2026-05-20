"""体育扫尾入场后的退出 overlay：一档 profit-take SELL 与资金占用效率门禁。"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from polymarket_trader.domain.sports_live import BaseballGameState, TennisGameState, VolleyballGameState
from polymarket_trader.extension_api import ExtensionContext

from strategies.current.config import CurrentStrategyConfig
from strategies.current.exit_plan import cap_price_to_clob_limit


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

    target_price = _profit_take_target_price(context, entry_price)
    if target_price is None or target_price > Decimal("1"):
        return None
    expected_profit_take_profit = shares * (target_price - entry_price)
    hold_minutes = max(int(config.tail_profit_take_hold_minutes), 1)
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


def _profit_take_target_price(context: ExtensionContext, entry_price: Decimal) -> Decimal | None:
    """返回买入价上方一档 tick 的 profit-take 目标价。"""

    tick_size = _effective_tick_size(context)
    if tick_size is None or tick_size <= Decimal("0"):
        tick_size = Decimal("0.01")
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
