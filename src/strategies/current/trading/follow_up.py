"""成交后续动作决策。

退出（止盈 / 止损 / 结算前平仓）现在统一由动态退出引擎 ``decide_exit``
负责：每个 reconcile 周期它都会从实时盘口重估 HOLD/EXIT 并在需要时下单。
此处不再在 BUY 成交后挂任何静态价 SELL，避免静态退出单覆盖持仓份额、
导致 ``decide_exit`` 因 ``no_uncovered_shares`` 跳过、动态引擎被旁路。

当前 BUY 成交后没有其他非退出类后续动作，函数返回 ``()``；保留函数与
hook 签名以维持扩展契约（与 ``discovery_queries_for_live_events``
"空但保契约" 的形态一致），后续若有非退出 follow-up 动作可在此扩展。
"""

from __future__ import annotations

from polymarket_trader.extension_api import ExtensionContext, ExtensionDecision

from strategies.current.config import CurrentStrategyConfig


def decide_follow_up(
    config: CurrentStrategyConfig,
    context: ExtensionContext,
) -> tuple[ExtensionDecision, ...]:
    """BUY 成交后的后续动作。退出由动态 ``decide_exit`` 负责，此处无动作。"""
    return ()
