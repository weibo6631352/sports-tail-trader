"""体育扫尾入场后的退出 overlay：一档 profit-take SELL 与资金占用效率门禁。"""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

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
    hold_minutes = max(int(config.tail_settlement_hold_minutes), 1)
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
