from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from polymarket_trader.domain.events import DomainEventType, EventEnvelope, OutboxEvent

logger = logging.getLogger(__name__)

# 已经 warn 过的未知 event_type（按 type 去重防刷屏）。多 worker 共用 sink，
# 第一次遇到时统一提醒；后续可在 operator 中查看实际丢弃量。
_unknown_event_types_warned: set[str] = set()

# 进库白名单:只持久化"产生订单 / 资金动作 / 人工干预"事件,其他全砍.
# decision_records 表已是全链路最全快照(decision_input 含 orderbook+信号+game_state),
# 复盘"为什么这一刻这么决策"完全够用,不需要 sports_live_state/allocation/reconcile
# 心跳类事件再写一份.
_PERSISTABLE_EVENT_TYPES = {
    # === 资金 / 持仓 / 订单(业务真相,必须留)===
    DomainEventType.BALANCE_UPDATED.value,
    DomainEventType.ORDER_STATE_UPDATED.value,
    DomainEventType.FILL_RECORDED.value,
    DomainEventType.POSITION_UPDATED.value,
    # === 市场终态(reconcile prune + settlement 依赖)===
    DomainEventType.MARKET_RESOLVED_OR_DISABLED.value,
    DomainEventType.MARKET_SETTLED.value,
    # === 风控 / 人工干预(合规审计必须)===
    DomainEventType.RISK_REJECTION_RECORDED.value,
    DomainEventType.TRADING_PAUSED.value,
    DomainEventType.TRADING_RESUMED.value,
    # === 订单生命周期(资金动作 + 合规审计必须)===
    # ORDER_STATE_UPDATED 是状态汇总通知, 不能替代下面这些具体动作记录;
    # 复盘"为什么这笔订单提交/被拒/成交"必须有单独 audit row.
    DomainEventType.ORDER_SUBMITTED.value,
    DomainEventType.ORDER_REJECTED.value,
    DomainEventType.ORDER_MATCHED.value,
    DomainEventType.ORDER_PARTIALLY_FILLED.value,
    DomainEventType.FOLLOW_UP_ORDER_SUBMITTED.value,
    DomainEventType.TRADE_MINED.value,
    DomainEventType.TRADE_CONFIRMED.value,
    # === 订单操作 intent(撤单/改单审计)===
    DomainEventType.ORDER_CANCEL_REQUESTED.value,
    DomainEventType.ORDER_CANCELLED.value,
    DomainEventType.REPLACE_ORDER_SUBMITTED.value,
    # === 单市场暂停(reconcile prune 链路 + 终态 audit endpoint 依赖)===
    DomainEventType.TRADING_PAUSED_FOR_MARKET.value,
    # === reconcile 真实差异/修复(operator /reconcile_diffs endpoint 依赖)===
    # 注意:RECONCILE_STARTED 是心跳已在 worker 层 throttle + persistence 层跳过,
    # 这里不收;DIFF_DETECTED/APPLIED 是真实修复记录必须留.
    DomainEventType.RECONCILE_DIFF_DETECTED.value,
    DomainEventType.RECONCILE_APPLIED.value,
    # === 砍掉(decision_records 已记决策上下文,无需重复写 audit)===
    # SPORTS_LIVE_STATE_RECORDED — 心跳,decision_snapshot 已含 live_state
    # ALLOCATION_DECISION_RECORDED — 决策评估,decision_records 已记
    # RECONCILE_STARTED — supervisor heartbeat 已记
    # MARKET_DISCOVERED / MARKET_UPDATED — 元数据流水
    # MARKET_FILTERED_IN / MARKET_FILTERED_OUT — 过滤流水
    # ORDERBOOK_DIRECTION_QUERIED — operator 读路径,无需 audit
}



class OutboxSink(Protocol):
    def put_nowait(self, event: OutboxEvent) -> bool: ...


def build_domain_event_outbox_sink(outbox: OutboxSink) -> Callable[[int, Any], None]:
    def _sink(priority: int, event: Any) -> None:
        outbox_event = _to_outbox_event(priority, event)
        if outbox_event is None:
            return
        outbox.put_nowait(outbox_event)

    return _sink


def _to_outbox_event(priority: int, event: Any) -> OutboxEvent | None:
    if not isinstance(event, EventEnvelope):
        return None
    event_type_text = str(event.event_type).strip()
    if not event_type_text:
        return None
    if event_type_text not in _PERSISTABLE_EVENT_TYPES:
        # 上游新增 event_type 但忘改 _PERSISTABLE_EVENT_TYPES 会导致事件静默丢失，
        # 上线后只能事后才发现「audit 漏了」。这里记一次 warning（按 type 去重避免刷屏）。
        if event_type_text not in _unknown_event_types_warned:
            _unknown_event_types_warned.add(event_type_text)
            logger.warning(
                "outbox_sink: dropping non-persistable event_type=%s (add to _PERSISTABLE_EVENT_TYPES if audit needed)",
                event_type_text,
            )
        return None
    payload = getattr(event, "payload", {})
    if not isinstance(payload, Mapping):
        payload = {}
    return OutboxEvent(
        trace_id=event.trace_id,
        event_type=event_type_text,
        idempotency_key=event.event_id,
        event_id=event.event_id,
        market_slug=_text(event.market_slug),
        event_slug=_text(event.event_slug),
        condition_id=_text(event.condition_id),
        token_id=_text(event.token_id),
        reason=_text(event.reason),
        created_at=_datetime(event.created_at),
        priority=priority,
        payload=_project_payload(event_type_text, payload),
    )


def _project_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """payload 透传 + 注入 trading_mode(paper/live).

    旧版有字段白名单裁剪,容易漏新字段静默丢失.改成默认透传,只附加
    trading_mode 区分 paper/live 复盘.
    """
    import os
    out = dict(payload)
    if "trading_mode" not in out:
        out["trading_mode"] = "paper" if os.getenv("PAPER_TRADING_MODE", "").lower() in ("1", "true", "yes") else "live"
    return out


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _datetime(value: Any) -> Any:
    return value
