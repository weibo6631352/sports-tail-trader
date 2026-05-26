"""audit_events 分层 retention 方案（原架构方案 §13.5）。

不同类别的 audit 事件价值不等——交易类需保留长（复盘 + 资金审计）；
拒绝/heartbeat 类只需短期（看趋势 + debug 边界）。本模块集中维护
event_title prefix → retention_days 映射，让 `app/audit_retention.py` 按
分层 cutoff 删除。

# 分层（§13.5）

| 类别 | 保留天数 | event_title 前缀 |
|---|---|---|
| 交易类 | 30 天 | order_created / order_signed / order_submitted / order_matched / fill_recorded / position_updated / balance_updated / replace_order_submitted |
| fills（独立） | **永久** | fill_recorded（资金审计，不删）— 与 trade 类区分 |
| 拒绝类 | 7 天 | risk_check_failed / risk_rejection_recorded / order_rejected / allocation_decision_recorded（含 rejected） / market_filtered_out |
| heartbeat / synthetic | 1 天 | position_heartbeat / sports_live_state_recorded / market_updated |
| 其他（默认）| 14 天 | 未列出的 event_title |

# 用法

```python
from polymarket_trader.infra.db.retention_policy import DEFAULT_AUDIT_RETENTION_POLICY

policy = DEFAULT_AUDIT_RETENTION_POLICY
days = policy.retention_days_for("order_created")  # 30
days_fill = policy.retention_days_for("fill_recorded")  # -1 (永久)
```

retention_days < 0 表示永不删；= 0 跳过（功能禁用语义）；> 0 按值删。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# fills 必须永久保留（金额审计）
PERMANENT_RETENTION_DAYS: int = -1

# 默认 retention（单位：天）；新增 event_title 落 PERMANENT / 14 兜底，
# 按上线 1-2 周观察数据频次调整对应 ttl
_DEFAULT_TIERS: tuple[tuple[tuple[str, ...], int], ...] = (
    # fills 永久（金额事件不允许丢）
    (("fill_recorded",), PERMANENT_RETENTION_DAYS),
    # 拒绝类：高频但短期价值（7d 看趋势够）
    (
        (
            "risk_check_failed",
            "risk_rejection_recorded",
            "order_rejected",
            "market_filtered_out",
        ),
        7,
    ),
    # heartbeat / synthetic / 状态更新类：1d 仅 debug 边界，不复盘
    (
        (
            "sports_live_state_recorded",
            "sports_live_match_gap_recorded",
            "market_updated",
        ),
        1,
    ),
    # 交易类：30d（订单全生命周期 + 持仓变化）
    (
        (
            "order_created",
            "order_signed",
            "order_submitted",
            "order_matched",
            "order_no_fill",
            "order_partially_filled",
            "order_state_updated",
            "order_cancel_requested",
            "order_cancelled",
            "replace_order_submitted",
            "follow_up_order_submitted",
            "position_updated",
            "balance_updated",
            "trade_mined",
            "trade_confirmed",
            "trading_paused",
            "trading_paused_for_market",
            "trading_resumed",
        ),
        30,
    ),
    # 决策审计类：30d
    (
        (
            "decision_recorded",
            "allocation_decision_recorded",
            "entry_signal_triggered",
            "market_settled",
            "reconcile_started",
            "reconcile_diff_detected",
            "reconcile_applied",
        ),
        30,
    ),
)

# 默认兜底（event_title 未匹配任何 tier）
_DEFAULT_FALLBACK_DAYS: int = 14


@dataclass(frozen=True, slots=True)
class AuditRetentionPolicy:
    """event_title → retention_days 分层查询。

    `tiers` 是 list of (prefix tuple, days)——按声明顺序查找首匹配。`fallback_days`
    在所有 tier 都不匹配时返回。
    """

    tiers: tuple[tuple[tuple[str, ...], int], ...] = field(default_factory=tuple)
    fallback_days: int = _DEFAULT_FALLBACK_DAYS

    def retention_days_for(self, event_title: str) -> int:
        """返回该 event_title 的 retention_days。

        - 正整数：按天数删除
        - 0：跳过（不删，但不阻拦 fallback 删）—— 当前无 tier 用 0
        - 负数（如 PERMANENT_RETENTION_DAYS）：永不删
        """

        for prefixes, days in self.tiers:
            for prefix in prefixes:
                if event_title.startswith(prefix):
                    return days
        return self.fallback_days

    def buckets_by_days(self) -> dict[int, tuple[str, ...]]:
        """返回 retention_days → event_title prefix 列表的反向映射，供分层 purge 用。"""

        result: dict[int, list[str]] = {}
        for prefixes, days in self.tiers:
            result.setdefault(days, []).extend(prefixes)
        return {days: tuple(prefixes) for days, prefixes in result.items()}


DEFAULT_AUDIT_RETENTION_POLICY = AuditRetentionPolicy(
    tiers=_DEFAULT_TIERS,
    fallback_days=_DEFAULT_FALLBACK_DAYS,
)


__all__ = [
    "PERMANENT_RETENTION_DAYS",
    "AuditRetentionPolicy",
    "DEFAULT_AUDIT_RETENTION_POLICY",
]
