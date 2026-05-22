"""体育扫尾入场的价格上限计算与锁定信号判定。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.extension_api import ExtensionContext, ExtensionPorts

from strategies.current.config import CurrentStrategyConfig
from strategies.current.outcomes import describe_sports_market, target_for_token
from strategies.current.parameter_overrides import effective_decimal


def _tail_price_cap(
    config: CurrentStrategyConfig,
    market,
    token_id: str | None,
    *,
    locked_outcome_signal: bool = False,
    ports: ExtensionPorts | None = None,
) -> Decimal:
    """计算入场价格上限。

    优先级：runtime override（``ports.parameter``）> frozen ``config``。
    ``ports=None`` 时行为与无 override 完全一致——保持旧测试和工具脚本兼容。
    """

    descriptor = describe_sports_market(market)
    entry_no_price_max = effective_decimal(
        ports, "entry_no_price_max", config.entry_no_price_max
    )
    if descriptor.market_type is None:
        return entry_no_price_max
    if token_id is not None and target_for_token(market, token_id) is None:
        return entry_no_price_max
    # 直播源已判定该市场为锁定候选（live_outcome_lock_candidate / ended_not_closed）——
    # 结果已经确定，放宽到锁定上限（任意盘口类型），把 0.97~0.995 这段确定性
    # 机会纳入入场；非锁定市场仍走下方各盘口类型的常规上限。
    if locked_outcome_signal:
        return config.tail_locked_outcome_max_entry_price
    if descriptor.market_type.value == "totals":
        return config.tail_totals_max_entry_price
    if descriptor.market_type.value == "moneyline":
        return effective_decimal(
            ports, "tail_moneyline_max_entry_price", config.tail_moneyline_max_entry_price
        )
    if descriptor.market_type.value == "spreads":
        return effective_decimal(
            ports, "tail_spreads_max_entry_price", config.tail_spreads_max_entry_price
        )
    return entry_no_price_max


def _tail_locked_outcome_signal(context: ExtensionContext) -> bool:
    """判断 live-state 是否已经标记当前市场为数学锁定候选。"""

    return str(context.metadata.get("entry_signal_reason") or "") in {
        "live_outcome_lock_candidate",
        "ended_not_closed",
    }
