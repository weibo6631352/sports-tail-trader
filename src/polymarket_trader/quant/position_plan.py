"""体育策略的持仓计划（position plan）元数据。

门禁过了即进场——本模块产出的 position_plan metadata 包含进场后所有行为：
止盈挂单价（exit price）、settlement 规则、异常状态处理、scale-in 通道等。
plan 本身不创建订单，只把策略对"持仓期如何决策"的意图写成稳定 metadata，
供 follow-up 卖出、reconcile、审计和管理台复盘使用。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from polymarket_trader.extension_api import ExtensionContext

from polymarket_trader.quant.config import CurrentStrategyConfig


POSITION_PLAN_VERSION = "1"


def build_position_plan_metadata(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    token_id: str | None,
    source_reason: str,
    target_size_shares: Decimal | None = None,
    entry_price: Decimal | None = None,
) -> dict[str, object]:
    """构造当前策略统一的持仓计划 metadata。

    参数：
        config:
            当前策略配置，提供默认退出价格。
        context:
            策略上下文，用于提取 market、trace 和已知比赛状态。
        token_id:
            计划对应的 token。
        source_reason:
            触发该计划的入场、跟单或恢复原因。
        target_size_shares:
            如已知卖出份额，则写入计划，便于后续复盘。
    """

    exit_price = exit_price_for_context(config, context, entry_price=entry_price)
    if config.auto_exit_enabled:
        primary_action = "place_follow_up_gtc_sell_after_buy_fill"
        settlement_rule = "keep_exit_order_until_fill_or_authoritative_resolution"
        recovery_rule = "cancel_open_entry_orders_and_cover_unprotected_positions"
    else:
        primary_action = "hold_until_authoritative_resolution"
        settlement_rule = "wait_for_authoritative_resolution_without_follow_up_sell"
        recovery_rule = "cancel_open_entry_orders_and_keep_position_for_settlement_or_manual_review"

    plan: dict[str, object] = {
        "version": POSITION_PLAN_VERSION,
        "source_reason": source_reason,
        "condition_id": None if context.market is None else context.market.condition_id,
        "market_slug": None if context.market is None else context.market.market_slug,
        "token_id": token_id,
        "target_exit_price": str(exit_price),
        "primary_action": primary_action,
        "settlement_rule": settlement_rule,
        "recovery_rule": recovery_rule,
        "abnormal_state_policy": {
            "stale_game_state": "pause_new_entries_until_live_state_refresh",
            "game_not_live": "pause_new_entries_and_keep_existing_exit_or_manual_review",
            "market_not_tradable": "pause_market_and_reconcile_open_orders",
            "exit_liquidity_missing": "keep_position_visible_for_manual_review",
        },
    }
    if target_size_shares is not None:
        plan["target_size_shares"] = str(target_size_shares)
    game_snapshot = _game_snapshot(context.metadata)
    if game_snapshot:
        plan["live_state"] = game_snapshot
    return {
        "position_plan_version": POSITION_PLAN_VERSION,
        "position_plan_source_reason": source_reason,
        "exit_target_price": str(exit_price),
        "position_plan": plan,
    }


def exit_price_for_context(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    entry_price: Decimal | None = None,
) -> Decimal:
    """返回符合当前 market tick size 的退出挂单价格。

    优先级（§17 准量化提前止盈）：
    1. 显式传 ``entry_price``（BUY 决策时已知本笔成交目标价）→ ``entry + offset``
    2. 持仓有 cost/shares 且 ``tail_profit_take_offset`` 配置了 → 返回 ``avg + offset``
       （盘中提前止盈可达：买 0.20 → 卖 0.27），不再死等结算
    3. 兜底返回 ``exit_no_price`` 锁定结算价（0.995→tick 后 0.99，无持仓信息时退化）

    超过 ``exit_no_price`` 的目标自动收敛到 ``exit_no_price``，避免越过 CLOB 上限或
    错过实际市场可成交价。
    """

    tick_size = _effective_tick_size(context)
    fallback = align_price_to_tick(config.exit_no_price, tick_size=tick_size)
    offset = config.tail_profit_take_offset
    if offset is None or offset <= Decimal("0"):
        return fallback
    base_price: Decimal | None = None
    if entry_price is not None and entry_price > Decimal("0"):
        base_price = entry_price
    else:
        position = context.position
        if position is not None and position.shares > Decimal("0") and position.cost_usdc > Decimal("0"):
            base_price = position.cost_usdc / position.shares
    if base_price is None:
        return fallback
    target = base_price + offset
    if target >= config.exit_no_price:
        return fallback
    aligned = align_price_to_tick(target, tick_size=tick_size)
    if aligned <= base_price:
        return fallback
    return aligned


def align_price_to_tick(price: Decimal, *, tick_size: Decimal | None) -> Decimal:
    """把策略目标价格向下对齐到交易所允许的 tick。

    卖出退出价是"目标上限"，当市场只支持 0.01 tick 时，0.995 应落到 0.99；
    如果 tick 缺失或异常，保持原价并交给框架风控继续审计。
    """

    if tick_size is None or tick_size <= Decimal("0"):
        return price
    units = (price / tick_size).to_integral_value(rounding=ROUND_FLOOR)
    if units <= 0:
        return price
    return units * tick_size


def cap_price_to_clob_limit(price: Decimal, *, tick_size: Decimal | None = None) -> Decimal:
    """把目标价限制在 Polymarket CLOB 当前 tick 接受的最高价格内。"""

    effective_tick = tick_size if tick_size is not None and tick_size > Decimal("0") else Decimal("0.01")
    return min(price, Decimal("1") - effective_tick)


def _effective_tick_size(context: ExtensionContext) -> Decimal | None:
    if context.orderbook is not None and context.orderbook.tick_size is not None:
        return context.orderbook.tick_size
    if context.market is not None:
        return context.market.tick_size
    return None


def _game_snapshot(metadata: Mapping[str, object]) -> dict[str, object]:
    raw_game = metadata.get("live_game")
    if not isinstance(raw_game, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in (
        "league",
        "home_name",
        "away_name",
        "home_score",
        "away_score",
        "period",
        "seconds_remaining",
        "status",
        "observed_at",
        "source",
        "source_event_id",
    ):
        value = raw_game.get(key)
        if value is not None:
            result[key] = value
    return result
