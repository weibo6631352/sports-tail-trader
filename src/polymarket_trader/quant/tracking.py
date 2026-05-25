"""当前策略对“被过滤市场是否继续保留跟踪”的规则。

这个文件处理的是 market 已经不再属于当前策略 universe 之后，
框架是否仍然保留 registry 记录和 WS 订阅。
"""

from __future__ import annotations

from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.contracts import AccountSnapshotView


def should_keep_tracking(
    market: Market,
    account_snapshot: AccountSnapshotView | None,
) -> bool:
    """判断一个已被过滤的 market 是否仍应继续跟踪。

    参数：
        market:
            当前被过滤的 market。
        account_snapshot:
            当前账户快照。没有快照时默认保守处理，继续保留跟踪。

    返回：
        ``True`` 表示继续保留 registry / 订阅；
        ``False`` 表示可以彻底移除。

    当前默认规则：
        - 只要还有持仓、open buy、open sell、pending buy，就继续保留；
        - 所有 exposure 清空后，才允许框架移除跟踪。
    """

    if account_snapshot is None:
        return True

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


def build_filtered_tracking_market(
    candidate_market: Market,
    *,
    existing_market: Market,
    reason: str,
) -> Market:
    """为“继续跟踪但已被策略排除”的 market 生成运行时状态。

    参数：
        candidate_market:
            这次 discovery 最新扫回来的 market 数据。
        existing_market:
            registry 中已存在的 market。
        reason:
            本次被策略排除的原因。

    返回：
        一个适合继续保留在运行时里的 ``Market``。

    规则：
        - 如果市场已经是 CLOSED / RESOLVED / REJECTED，就保留终态；
        - 否则统一把交易状态改成 ``PAUSED``，并写入 reject reason。
    """

    if existing_market.trading_status in {
        TradingStatus.CLOSED,
        TradingStatus.RESOLVED,
        TradingStatus.REJECTED,
    }:
        return candidate_market.with_trading_status(
            existing_market.trading_status,
            reject_reason=existing_market.reject_reason,
        )
    return candidate_market.with_trading_status(
        TradingStatus.PAUSED,
        reject_reason=reason or "market_out_of_universe",
    )
