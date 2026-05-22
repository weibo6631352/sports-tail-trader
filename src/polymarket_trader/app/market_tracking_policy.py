"""运行时 market 跟踪保留策略。

该模块只判断一个 market 是否还值得留在 registry / WS 跟踪集合中，不负责
交易决策、下单或前端展示。调用方必须在移除前确认没有账户风险敞口。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market, TradingStatus

# Polymarket sports 市场的 end_date 通常等于 game_start_time（赛事开始时间），而非赛事真正结束时间。
# 为保证在比赛进行中（最长 ~4h）仍持续跟踪，跟 end_date 比较时加 6h 宽限期。
_END_DATE_GRACE = timedelta(hours=6)

# 单场赛事 WS 跟踪时间窗口：只有进行中/临近开赛的赛事才纳入 WS 订阅——
# 避免 discovery 一次性 track 上万个远期市场、market WS 订阅永远追不上。
# 过去 6h 覆盖仍在进行中的比赛；未来 30 分钟覆盖临近开赛（留足 discovery
# 重新评估的余量）。窗口外市场由后续 discovery 轮次在赛事临近时重新纳入。
_TRADE_WINDOW_PAST = timedelta(hours=6)
_TRADE_WINDOW_FUTURE = timedelta(minutes=30)


def market_outside_trade_window(market: Market, *, now: datetime) -> bool:
    """单场赛事市场是否在 WS 跟踪时间窗口之外（远期未开赛 / 早已结束）。

    无开赛时间（outright / futures / 系列赛等）无法判定 → 返回 False，
    不因数据缺失误排除。
    """

    start = market.game_start_time
    if start is None:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return start > now + _TRADE_WINDOW_FUTURE or start < now - _TRADE_WINDOW_PAST

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
    """判断 market 的结束时间（含宽限期）是否已经过去，兼容 naive datetime。

    宽限期见 _END_DATE_GRACE：sports 市场 end_date = game_start_time，宽限期让赛事全程被跟踪。
    """

    if market.end_date is None:
        return False
    market_end = market.end_date
    if market_end.tzinfo is None:
        market_end = market_end.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (market_end + _END_DATE_GRACE).astimezone(timezone.utc) <= now.astimezone(timezone.utc)


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
