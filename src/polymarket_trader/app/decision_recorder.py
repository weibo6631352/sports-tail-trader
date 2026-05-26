"""决策录制 producer 侧 helper。

workflow hook 返回 ``TradingDecision`` 后，framework 在主链路同步构造
``DecisionRecord`` 并经 outbox 投递；``PersistenceWorker`` 异步落库到
``decision_records`` 表。

P0 热路径硬约束（CLAUDE.md §3 / §7）：
- 不允许 ``await`` 数据库；只允许同步 ``outbox.put_nowait``。
- 序列化失败必须吞掉异常，不阻塞决策返回。
- 拒绝原因 / accepted 仅复用现有决策字段，不发明新枚举值。

state-change dedup
==================

同一 (condition_id, token_id, hook_name) 上次 emit 的决策与本次输出一致
（action / price / amount / size / order_id / decision_kind / reason 全部
相同）→ 视为"交易状态未变化"——不写 DB,不进 outbox。

quant_decider 每 tick 评估同一 candidate 反复返回相同 BUY intent / 同一
SKIP reason 时,decision_records 表 1:1 暴涨（实测每秒 ~21 条）。只在状态
真发生变化（BUY→SKIP / 价格变 / 金额变 / 不同 reason）时写一条,大幅减量。
首次 emit 视为状态从无到有,必写。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Protocol

from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import DomainEventType, OutboxEvent, OutboxPriority
from polymarket_trader.serialization import jsonable

logger = logging.getLogger(__name__)


# state-change dedup 取这些字段作为 "交易状态" 指纹——任何变化都视为状态变。
# 其他字段（intent_tags / metadata / summary）属于辅助上下文,不影响"状态"判定。
_STATE_FIELDS: tuple[str, ...] = (
    "action",
    "decision_kind",
    "reason",
    "price",
    "amount_usdc",
    "size_shares",
    "order_id",
    "order_type",
    "post_only",
)


def _state_fingerprint(decision_output: Mapping[str, Any]) -> str:
    """从 decision_output 抽取 state 指纹——稳定 JSON 序列化后字符串作 key。"""
    state = {k: decision_output.get(k) for k in _STATE_FIELDS}
    return json.dumps(state, sort_keys=True, default=str)


class _OutboxSink(Protocol):
    def put_nowait(self, event: OutboxEvent) -> bool: ...


class DecisionEventRecorder:
    """同步把量化决策投递到 outbox。

    暴露 ``record(...)`` 接口供 DecisionContextBuilder / ReconcileService 旁路调用,
    内部维护 ``(cid, tid, hook) → 上次 state 指纹`` 的 dedup 缓存——只在状态变化时
    构造 OutboxEvent + ``put_nowait``。

    缓存按 condition_id 实现 ``MarketScopedStore`` 协议,跟随 market lifecycle evict。
    """

    __slots__ = ("_outbox", "_last_state")

    def __init__(self, outbox: _OutboxSink | None) -> None:
        self._outbox = outbox
        self._last_state: dict[tuple[str, str | None, str | None], str] = {}

    def record(self, record: DecisionRecord) -> None:
        if self._outbox is None:
            return
        # state-change dedup:同 (cid, tid, hook) 上次 emit 的关键字段全相同 → 状态未变,
        # 不写。BUY→SKIP / 价格变 / reason 变 / 首次 emit 都视为状态变化必写。
        # decision_records 表此前 2 小时积累 151K 行,绝大多数是同一 candidate
        # 反复评估返回相同 intent;dedup 后只在状态真变时落一条。
        key = (record.condition_id, record.token_id, record.hook_name)
        fingerprint = _state_fingerprint(record.decision_output)
        if self._last_state.get(key) == fingerprint:
            return
        self._last_state[key] = fingerprint
        log_context = {
            "trace_id": record.trace_id,
            "hook_name": record.hook_name,
            "condition_id": record.condition_id,
        }
        try:
            event = _build_outbox_event(record)
        except Exception:
            # P0 路径序列化失败不能影响决策返回，但必须带上 trace_id / hook_name
            # 以便运维侧反查（§10 可审计性）——只 logger.debug 会让丢失的决策完全
            # 不可追踪。
            logger.warning(
                "decision recorder serialize failed",
                exc_info=True,
                extra=log_context,
            )
            return
        try:
            self._outbox.put_nowait(event)
        except Exception:
            # outbox 满 / 异常一律吞掉；DB 不是交易真相，丢一条不能拖停交易。
            logger.warning(
                "decision recorder outbox put_nowait failed",
                exc_info=True,
                extra=log_context,
            )
            return

    # ---------- MarketScopedStore 协议 ----------

    def evict_market(self, condition_id: str, token_ids: tuple[str, ...]) -> None:
        """market lifecycle 结束时清缓存,防内存泄漏。

        清掉该 cid 下所有 (token_id, hook_name) 维度的 fingerprint 缓存。
        """
        del token_ids
        # 不能在迭代时改 dict——先收集 key 再删除
        keys_to_remove = [k for k in self._last_state if k[0] == condition_id]
        for k in keys_to_remove:
            self._last_state.pop(k, None)


def build_decision_record_from_hook(
    *,
    hook_name: str,
    trace_id: str,
    context: Any,
    decision: Any,
    condition_id: str | None,
    token_id: str | None = None,
    market_slug: str | None = None,
) -> DecisionRecord | None:
    """把 hook 调用入参与返回值收敛成 ``DecisionRecord``。

    ``condition_id`` 是 DB 索引的强字段；调用侧拿不到时返回 None，让 framework
    选择跳过录制。``hook_name`` 区分 audit 录入的来源——当前只有 ``quant_decide``，
    后续接入更多 hook（如 ``match_live_state``）也走同一份录入函数。
    """

    if not condition_id:
        return None
    context_payload = _safe_jsonable(context)
    decision_payload = _safe_jsonable(decision)
    accepted, reason = _decision_outcome(decision_payload)
    return DecisionRecord(
        trace_id=trace_id,
        condition_id=str(condition_id),
        hook_name=hook_name,
        token_id=token_id,
        market_slug=market_slug,
        decision_input=context_payload,
        decision_output=decision_payload,
        accepted=accepted,
        reason=reason,
    )


def _safe_jsonable(value: Any) -> dict[str, Any]:
    try:
        payload = jsonable(value)
    except Exception as exc:
        return {"_serialize_error": str(exc)}
    if isinstance(payload, Mapping):
        return dict(payload)
    return {"value": payload}


def _decision_outcome(payload: Mapping[str, Any]) -> tuple[bool, str | None]:
    """从 ``TradingDecision`` 投影出 ``accepted`` 与 ``reason``。

    accepted 语义：量化决策器返回的 ``decision_kind`` 表示"产生了可执行 intent"
    即视为接受；任何 skip / decline / no_signal 等已存在的字符串都保留原值
    放进 reason，不发明新枚举。
    """

    if not isinstance(payload, Mapping):
        return False, None
    reason_value = payload.get("reason")
    reason: str | None
    if reason_value is None:
        reason = None
    else:
        text = str(reason_value).strip()
        reason = text or None
    action = payload.get("action")
    decision_kind = payload.get("decision_kind")
    if isinstance(action, str) and action.strip().lower() in {"skip", "decline", "noop", "no_action"}:
        return False, reason
    if isinstance(decision_kind, str) and decision_kind.strip().lower() in {"skip", "decline"}:
        return False, reason
    # tuple of decisions（decide_follow_up 返回 tuple）单独处理；payload 已经被
    # jsonable 投成 list/dict。空 tuple = 量化决策器主动选择不产生 follow-up，明确
    # 落 reason 防止 audit 显示「拒绝且无原因」的歧义（§10 可审计性）。
    if isinstance(action, list):
        if not action:
            return False, reason or "no_follow_ups"
        return True, reason
    if action is None and decision_kind is None and "value" in payload:
        # 包装值——通常是 follow_up 的 tuple 投影或简单类型。
        inner = payload.get("value")
        if isinstance(inner, list) and not inner:
            return False, reason or "no_follow_ups"
        return bool(inner), reason
    return action is not None or decision_kind is not None, reason


def _build_outbox_event(record: DecisionRecord) -> OutboxEvent:
    payload = {
        "record_id": record.record_id,
        "trace_id": record.trace_id,
        "hook_name": record.hook_name or "",
        "condition_id": record.condition_id,
        "token_id": record.token_id,
        "market_slug": record.market_slug,
        "decision_input": dict(record.decision_input),
        "decision_output": dict(record.decision_output),
        "accepted": bool(record.accepted),
        "reason": record.reason,
        "created_at": record.created_at,
    }
    return OutboxEvent(
        trace_id=record.trace_id,
        event_type=DomainEventType.DECISION_RECORDED.value,
        idempotency_key=f"decision:{record.record_id}",
        event_id=record.record_id,
        market_slug=record.market_slug,
        condition_id=record.condition_id,
        token_id=record.token_id,
        reason=record.reason,
        created_at=record.created_at,
        priority=int(OutboxPriority.P3.value[1:]),
        payload=payload,
    )


__all__ = [
    "DecisionEventRecorder",
    "build_decision_record_from_hook",
]
