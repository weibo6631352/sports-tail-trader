"""运行时 market 跟踪保留策略。

该模块只判断一个 market 是否还值得留在 registry / WS 跟踪集合中，不负责
交易决策、下单或前端展示。调用方必须在移除前确认没有账户风险敞口。
"""

from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market, TradingStatus

TERMINAL_LIVE_STATE_PAUSE_REASONS = frozenset(
    {
        "sports_live_state_cancelled",
        "sports_live_state_ended",
        "sports_live_state_retired",
    }
)


def market_unsubscribe_prune_reason(
    account_snapshot: AccountSnapshot | None,
    market: Market,
    *,
    now: datetime,
) -> str | None:
    """返回无敞口 market 可退出订阅的原因。

    没有账户快照时保守返回 ``None``，避免在无法确认持仓和挂单的情况下
    误删仍需恢复或退出的市场。
    """

    if account_snapshot is None:
        return None
    if market_has_exposure(account_snapshot, market):
        return None
    return inactive_market_reason(account_snapshot, market, now=now)


def inactive_market_reason(
    account_snapshot: AccountSnapshot,
    market: Market,
    *,
    now: datetime,
) -> str | None:
    """判断 market 是否已经进入无需继续订阅的终态。"""

    if market.trading_status in {TradingStatus.CLOSED, TradingStatus.RESOLVED}:
        return f"market_{market.trading_status.value}"
    if market.trading_status == TradingStatus.PAUSED and market.reject_reason:
        # 无敞口的 PAUSED market 只保留审计拒绝原因，不再占用运行时跟踪集合。
        # 后续 discovery 若重新变成可交易，会按目标策略重新纳入 registry。
        return market.reject_reason
    if market_end_date_elapsed(market, now=now):
        return "market_end_date_elapsed"
    account_pause = account_snapshot.pause_for_market(market.condition_id)
    if account_pause is not None and account_pause.reason in TERMINAL_LIVE_STATE_PAUSE_REASONS:
        return account_pause.reason
    return None


def market_end_date_elapsed(market: Market, *, now: datetime) -> bool:
    """判断 market 的结束时间是否已经过去，兼容 naive datetime。"""

    if market.end_date is None:
        return False
    market_end = market.end_date
    if market_end.tzinfo is None:
        market_end = market_end.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return market_end.astimezone(timezone.utc) <= now.astimezone(timezone.utc)


def market_has_exposure(account_snapshot: AccountSnapshot, market: Market) -> bool:
    """判断账户在某个 market 上是否仍有持仓、挂单或 pending buy。"""

    for token_id in market.token_ids:
        position = account_snapshot.get_position(market.condition_id, token_id)
        if position is not None and (
            position.shares > 0
            or position.open_buy_shares > 0
            or position.open_sell_shares > 0
            or position.pending_buy_shares > 0
        ):
            return True
        if account_snapshot.open_orders_for_market(market.condition_id, token_id):
            return True
    return False
