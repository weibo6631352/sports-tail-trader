"""量化决策器——Workflow 2 所有 WS / 周期触发的决策统一入口。

按 ``context.quant_trigger_kind`` 分派到内部子流程：
- ``orderbook_tick``：盘口事件 → SELL 价跟随（_decide_orderbook_tick）
- ``order_fill``：成交事件 → 无后续动作（当前策略让动态 SELL 链路接管）
- ``reconcile_cycle``：周期扫账户 → 清理僵尸订单 / 历史高成本 profit-take / pause 信号

所有子流程的输出统一包装成 QuantDecision；reconcile_cycle 可能附带 pause_trading 信号。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from datetime import timezone

from polymarket_trader.extension_api import ExtensionContext, ExtensionDecision, QuantDecision

from strategies.current.config import CurrentStrategyConfig

from .exit_overlay import evaluate_dynamic_exit
from .helpers import _metadata_text


def quant_decide(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> QuantDecision:
    trigger = context.quant_trigger_kind
    if trigger == "orderbook_tick":
        decision = _decide_orderbook_tick(config, context)
        if decision.action.value == "skip":
            return QuantDecision(actions=(), reason=decision.reason)
        return QuantDecision(actions=(decision,), reason=decision.reason)
    if trigger == "order_fill":
        # BUY 成交后无静态 follow-up：让 orderbook_tick 路径接管 SELL 决策。
        return QuantDecision(actions=(), reason="order_fill_no_follow_up")
    if trigger == "reconcile_cycle":
        from strategies.current.recovery import build_recovery_quant_decision
        return build_recovery_quant_decision(config, context)
    return QuantDecision(actions=(), reason=f"unknown_trigger_kind:{trigger}")


def _decide_orderbook_tick(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> ExtensionDecision:
    """盘口事件触发：算 SELL 价跟随（原 decide_exit 路径）。"""

    from .hooks import _maybe_reprice_stale_sell, _position_entry_price, _utc_now
    from strategies.current.position_plan import build_position_plan_metadata, exit_price_for_context

    if not config.auto_exit_enabled:
        return ExtensionDecision.skip(reason="settlement_only_exit_disabled")

    now = context.now or _utc_now()
    if context.account_snapshot is not None:
        last_reconcile = context.account_snapshot.last_reconcile_at
        if last_reconcile is None:
            return ExtensionDecision.skip(reason="reconcile_never_completed")
        if last_reconcile.tzinfo is None:
            last_reconcile = last_reconcile.replace(tzinfo=timezone.utc)
        age = (now.astimezone(timezone.utc) - last_reconcile.astimezone(timezone.utc)).total_seconds()
        if age > 60.0:
            return ExtensionDecision.skip(
                reason="reconcile_stale_skip_exit",
                metadata={"reconcile_age_seconds": str(age)},
            )

    size_shares = context.size_shares
    if size_shares is not None and size_shares > Decimal("0"):
        uncovered_shares = size_shares
    elif context.position is not None:
        uncovered_shares = context.position.shares - context.position.open_sell_shares
    else:
        return ExtensionDecision.skip(reason="missing_position_state")

    min_meaningful_uncovered = Decimal("0.1")
    if uncovered_shares < min_meaningful_uncovered:
        replace_decision = _maybe_reprice_stale_sell(config, context, now=now)
        if replace_decision is not None:
            return replace_decision
        return ExtensionDecision.skip(
            reason="no_uncovered_shares",
            metadata={"uncovered_shares": str(uncovered_shares)},
        )

    if (
        context.position is not None
        and (context.position.current_value is None or context.position.current_value <= Decimal("0"))
        and context.orderbook is None
    ):
        return ExtensionDecision.skip(reason="position_zero_value_no_orderbook")

    token_id = (
        context.token_id
        or (context.position.token_id if context.position is not None else None)
        or _metadata_text(context, "token_id")
    )
    entry_price = _position_entry_price(context)
    decision_metadata = build_position_plan_metadata(
        config,
        context,
        token_id=token_id,
        source_reason="quant_exit",
        target_size_shares=uncovered_shares,
        entry_price=entry_price,
    )

    exit_price = exit_price_for_context(config, context, entry_price=entry_price)
    exit_reason = "quant_exit"
    if entry_price is not None:
        dynamic = evaluate_dynamic_exit(
            config,
            context,
            token_id=token_id,
            entry_price=entry_price,
        )
        if dynamic is not None:
            decision_metadata.update(dynamic.metadata)
            if not dynamic.should_exit:
                return ExtensionDecision.skip(
                    reason=dynamic.reason,
                    metadata=decision_metadata,
                )
            assert dynamic.exit_price is not None
            exit_price = dynamic.exit_price
            exit_reason = dynamic.reason
            decision_metadata["exit_target_price"] = str(exit_price)
            plan = decision_metadata.get("position_plan")
            if isinstance(plan, dict):
                plan["target_exit_price"] = str(exit_price)

    if exit_price is not None and context.orderbook is not None:
        tick = context.orderbook.tick_size or Decimal("0.01")
        if tick > Decimal("0"):
            exit_price = (exit_price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    return ExtensionDecision.sell(
        reason=exit_reason,
        token_id=token_id,
        price=exit_price,
        size_shares=uncovered_shares,
        market_slug=(
            context.market.market_slug if context.market is not None else _metadata_text(context, "market_slug")
        ),
        metadata=decision_metadata,
    )
