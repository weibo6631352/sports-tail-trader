"""LiveSignalSnapshot —— 决策时刻的真概率信号快照（observability + 复盘）。

每次 ``workflow.quant_signal.estimate_signal`` 评估都会产出一份 snapshot，被
``app/signal_snapshot_store`` 写入 ring buffer。下游 ``/analytics/edge-signals``
endpoint 暴露 top-K 差价候选（按 ``edge_pp`` 排序），让 operator 实时看到
"哪些 condition pm_best_ask 远低于 goalserve fair_prob = 入场 edge 机会"。

CPO Round 2 R6 范围（情况 A）：仅做 observability，**不**用 snapshot 直接驱动
决策——决策仍由 quant_signal.estimate_signal → Kelly 链路负责。未来 R6+ 可能
基于这些 snapshot 数据决定信号源权重（如发现 goalserve_fair_prob 历史命中率高
于 math_prob → 加权）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class LiveSignalSnapshot:
    """决策时刻的真概率信号 + 盘口快照。

    字段语义：
    - ``condition_id`` / ``token_id``：被评估的 market + outcome token
    - ``pm_best_ask`` / ``pm_best_bid``：Polymarket 当时盘口（None = 缺）
    - ``goalserve_fair_prob``：去 vig 后的博彩 implied prob（None = 缺数据/盘口暂停）
    - ``math_prob``：sport-specific 数学概率（None = 该 sport 无公式或缺直播状态）
    - ``microprice``：order_book 微价（None = 缺单边深度）
    - ``final_prob_p``：estimate_signal 最终输出（喂 Kelly 用）
    - ``source_used``：哪一层产出 final_prob_p（如 ``"goalserve_implied_prob"`` /
      ``"math_prob"`` / ``"microprice"`` / ``"market_mid"`` / ``"best_bid"``）
    - ``edge_pp``：``pm_best_ask`` 与 ``goalserve_fair_prob`` 的差值（pp），正值
      = 入场 edge 候选（市场低估赢方）；None = ask 或 goalserve 缺
    - ``timestamp``：snapshot 产出时刻（UTC）
    """

    condition_id: str
    token_id: str
    pm_best_ask: Decimal | None
    pm_best_bid: Decimal | None
    goalserve_fair_prob: Decimal | None
    math_prob: Decimal | None
    microprice: Decimal | None
    final_prob_p: Decimal
    source_used: str
    edge_pp: Decimal | None
    timestamp: datetime
    # R15 (架构师 Round 7 决议主线 c)：决策时刻 bankroll 数据新鲜度 = now - last_reconcile_at
    # 秒数。决策 P99 必须 ≤ 25s（与 market_sync_interval_seconds=20s 一致）；超过说明
    # bankroll 拉取链路滞后或断流，喂 Kelly 的 portfolio_budget_usdc 已 stale，是 P0
    # 归因黑洞。None = AccountSnapshot 缺 last_reconcile_at（启动早期 / 测试 fixture）。
    account_age_s: Decimal | None = None


__all__ = ["LiveSignalSnapshot"]
