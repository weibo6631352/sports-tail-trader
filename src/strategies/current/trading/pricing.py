"""体育扫尾入场的价格上限计算与锁定信号判定。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.extension_api import ExtensionContext

from strategies.current.config import CurrentStrategyConfig
from strategies.current.outcomes import describe_sports_market, target_for_token


def _sports_tail_price_cap(
    config: CurrentStrategyConfig,
    market,
    token_id: str | None,
    *,
    locked_outcome_signal: bool = False,
) -> Decimal:
    descriptor = describe_sports_market(market)
    if descriptor.market_type is None:
        return config.entry_no_price_max
    if token_id is not None and target_for_token(market, token_id) is None:
        return config.entry_no_price_max
    if locked_outcome_signal and descriptor.market_type.value == "moneyline" and _is_tennis_set_winner_market(market):
        return config.sports_tennis_locked_moneyline_max_entry_price
    if descriptor.market_type.value == "totals":
        return config.sports_totals_max_entry_price
    if descriptor.market_type.value == "moneyline":
        return config.sports_moneyline_max_entry_price
    if descriptor.market_type.value == "spreads":
        return config.sports_spreads_max_entry_price
    return config.entry_no_price_max


def _sports_tail_locked_outcome_signal(context: ExtensionContext) -> bool:
    """判断 live-state 是否已经标记当前市场为数学锁定候选。"""

    return str(context.metadata.get("sports_tail_entry_signal_reason") or "") in {
        "live_outcome_lock_candidate",
        "ended_not_closed",
    }


def _is_tennis_set_winner_market(market) -> bool:
    text = str(getattr(market, "market_slug", "") or "").strip().lower().replace("_", " ").replace("-", " ")
    return "set winner" in text or "first set winner" in text
