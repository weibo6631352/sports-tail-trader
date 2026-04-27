"""体育扫尾策略的退出计划元数据。

退出计划不直接创建订单，只把策略对“买入后如何卖出、异常时如何处理”的意图
写成稳定 metadata，供跟单卖出、reconcile、审计和管理台复盘使用。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from polymarket_trader.extension_api import ExtensionContext

from strategies.current.config import CurrentStrategyConfig


EXIT_PLAN_VERSION = "1"


def build_exit_plan_metadata(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
    *,
    token_id: str | None,
    source_reason: str,
    target_size_shares: Decimal | None = None,
) -> dict[str, object]:
    """构造当前策略统一的退出计划 metadata。

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

    plan: dict[str, object] = {
        "version": EXIT_PLAN_VERSION,
        "source_reason": source_reason,
        "condition_id": None if context.market is None else context.market.condition_id,
        "market_slug": None if context.market is None else context.market.market_slug,
        "token_id": token_id,
        "target_exit_price": str(config.exit_no_price),
        "primary_action": "place_follow_up_gtc_sell_after_buy_fill",
        "settlement_rule": "keep_exit_order_until_fill_or_authoritative_resolution",
        "recovery_rule": "cancel_open_entry_orders_and_cover_unprotected_positions",
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
        "sports_exit_plan_version": EXIT_PLAN_VERSION,
        "sports_exit_target_price": str(config.exit_no_price),
        "sports_exit_source_reason": source_reason,
        "sports_exit_plan": plan,
    }


def _game_snapshot(metadata: Mapping[str, object]) -> dict[str, object]:
    raw_game = metadata.get("sports_tail_game")
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
