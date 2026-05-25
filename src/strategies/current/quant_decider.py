"""量化决策器类——所有持仓期决策的单一入口。

按 ``context.quant_trigger_kind`` 分派子流程：
- ``market_tick``：market_ws book / price_change 触发的盘口事件 → 当前实现做 SELL 价跟随
- ``reconcile_cycle``：周期扫账户 → 清理僵尸订单 / pause 信号

未来扩展：market_tick 路径也可产出 BUY（量化交易真正形态），与 entry_planner
入场链路合并；目前 BUY 仍走 ``size_entry`` + ``decide_entry``，由
``entry_planner`` 编排。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from datetime import timezone, datetime

from polymarket_trader.extension_api import (
    ExtensionContext,
    ExtensionDecision,
    ExtensionPorts,
    QuantDecision,
)

from strategies.current.config import CurrentStrategyConfig


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QuantDecider:
    """量化决策器。

    所有 WS 盘口 tick / reconcile 周期触发的"持仓后该怎么动"决策都从这里出。
    类内不持有可变运行时状态（每次 ``decide`` 调用从 context 拿最新快照），
    保留 ``ports`` 引用便于未来接入 parameter store / metrics。
    """

    def __init__(
        self,
        *,
        config: CurrentStrategyConfig,
        ports: ExtensionPorts | None = None,
    ) -> None:
        self._config = config
        self._ports = ports

    # ---- 主入口 -----------------------------------------------------

    def decide(self, context: ExtensionContext) -> QuantDecision:
        trigger = context.quant_trigger_kind
        if trigger == "market_tick":
            return self._decide_market_tick(context)
        if trigger == "reconcile_cycle":
            return self._decide_reconcile(context)
        return QuantDecision(actions=(), reason=f"unknown_trigger_kind:{trigger}")

    # ---- market_tick：盘口事件路径 ----------------------------------

    def _decide_market_tick(self, context: ExtensionContext) -> QuantDecision:
        """市场盘口事件触发：算 SELL 价跟随。

        当前仅处理已持仓 token 的 SELL/replace 决策；BUY 入场仍由
        entry_planner 走 size_entry / decide_entry。等下一步迁移完，BUY 也归这里。
        """
        decision = self._decide_position_action(context)
        if decision.action.value == "skip":
            return QuantDecision(actions=(), reason=decision.reason)
        return QuantDecision(actions=(decision,), reason=decision.reason)

    def _decide_position_action(self, context: ExtensionContext) -> ExtensionDecision:
        from strategies.current.trading.hooks import _maybe_reprice_stale_sell, _position_entry_price
        from strategies.current.trading.exit_overlay import evaluate_dynamic_exit
        from strategies.current.trading.helpers import _metadata_text
        from strategies.current.position_plan import (
            build_position_plan_metadata,
            exit_price_for_context,
        )

        config = self._config
        if not config.auto_exit_enabled:
            return ExtensionDecision.skip(reason="settlement_only_exit_disabled")

        now = context.now or _utc_now()
        if context.account_snapshot is not None:
            last_reconcile = context.account_snapshot.last_reconcile_at
            if last_reconcile is None:
                return ExtensionDecision.skip(reason="reconcile_never_completed")
            if last_reconcile.tzinfo is None:
                last_reconcile = last_reconcile.replace(tzinfo=timezone.utc)
            age = (
                now.astimezone(timezone.utc) - last_reconcile.astimezone(timezone.utc)
            ).total_seconds()
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

        if uncovered_shares < Decimal("0.1"):
            replace_decision = _maybe_reprice_stale_sell(config, context, now=now)
            if replace_decision is not None:
                return replace_decision
            return ExtensionDecision.skip(
                reason="no_uncovered_shares",
                metadata={"uncovered_shares": str(uncovered_shares)},
            )

        if (
            context.position is not None
            and (
                context.position.current_value is None
                or context.position.current_value <= Decimal("0")
            )
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
                context.market.market_slug
                if context.market is not None
                else _metadata_text(context, "market_slug")
            ),
            metadata=decision_metadata,
        )

    # ---- reconcile_cycle：周期路径 ----------------------------------

    def _decide_reconcile(self, context: ExtensionContext) -> QuantDecision:
        from strategies.current.recovery import build_recovery_quant_decision
        return build_recovery_quant_decision(self._config, context)
