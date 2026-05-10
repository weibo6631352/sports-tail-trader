"""基于 ``DecisionRecord`` 的策略回放与对比。

工作流：
    1. 生产 / 测试时通过 ``InMemoryDecisionRecorder`` 录制实盘决策
    2. 改完一行策略代码后，在 ReplayHarness 上灌入旧录制 + 新 hooks
    3. ReplayHarness 比对新旧 decision_payload，输出每条 record 的 diff 分类

这是一个**离线复盘工具**，不进 P0 主链路。它故意不重建 ``ExtensionContext`` 对象，
而是直接给新 hooks 一个能反映原始 context 的轻量 ``ReplayContext``——这样开发者
即使改了 Context 的字段顺序也能 replay 历史录制（结构性兼容兜底，避免每改一次
context 就让整个录制库失效）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping

from polymarket_trader.extension_api.recorder import DecisionRecord


class ReplayDiffKind(StrEnum):
    """新旧决策的对比分类。"""

    UNCHANGED = "unchanged"
    REASON_CHANGED = "reason_changed"
    ACTION_CHANGED = "action_changed"
    PRICE_CHANGED = "price_changed"
    AMOUNT_CHANGED = "amount_changed"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ReplayDiff:
    record: DecisionRecord
    new_decision_payload: Mapping[str, Any]
    kind: ReplayDiffKind
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReplayReport:
    diffs: tuple[ReplayDiff, ...]

    @property
    def changed_count(self) -> int:
        return sum(1 for d in self.diffs if d.kind != ReplayDiffKind.UNCHANGED)

    @property
    def by_kind(self) -> dict[ReplayDiffKind, int]:
        counts: dict[ReplayDiffKind, int] = {kind: 0 for kind in ReplayDiffKind}
        for diff in self.diffs:
            counts[diff.kind] += 1
        return counts


# 调用方提供的"基于 record + 新 hooks 重算 decision payload"的回调。
# 让 ReplayHarness 不直接 import 策略框架细节——调用方决定如何重建 context。
ReplayDecisionFn = Callable[[DecisionRecord], Mapping[str, Any]]


def replay_records(
    records: Iterable[DecisionRecord],
    *,
    replay_decision: ReplayDecisionFn,
) -> ReplayReport:
    """对每条 record 调一次 ``replay_decision``，与原 decision_payload 对比。"""

    diffs: list[ReplayDiff] = []
    for record in records:
        try:
            new_payload = dict(replay_decision(record))
        except Exception as exc:
            new_payload = {"_replay_error": str(exc)}
        diffs.append(_classify(record, new_payload))
    return ReplayReport(diffs=tuple(diffs))


def _classify(record: DecisionRecord, new_payload: Mapping[str, Any]) -> ReplayDiff:
    old = dict(record.decision_payload)
    if "_replay_error" in new_payload:
        return ReplayDiff(
            record=record,
            new_decision_payload=new_payload,
            kind=ReplayDiffKind.OTHER,
            detail=str(new_payload["_replay_error"]),
        )
    if old == new_payload:
        return ReplayDiff(record=record, new_decision_payload=new_payload, kind=ReplayDiffKind.UNCHANGED)
    if old.get("action") != new_payload.get("action"):
        return ReplayDiff(
            record=record,
            new_decision_payload=new_payload,
            kind=ReplayDiffKind.ACTION_CHANGED,
            detail=f"{old.get('action')} -> {new_payload.get('action')}",
        )
    if old.get("price") != new_payload.get("price"):
        return ReplayDiff(
            record=record,
            new_decision_payload=new_payload,
            kind=ReplayDiffKind.PRICE_CHANGED,
            detail=f"{old.get('price')} -> {new_payload.get('price')}",
        )
    if old.get("amount_usdc") != new_payload.get("amount_usdc") or old.get("size_shares") != new_payload.get("size_shares"):
        return ReplayDiff(
            record=record,
            new_decision_payload=new_payload,
            kind=ReplayDiffKind.AMOUNT_CHANGED,
            detail=(
                f"amount {old.get('amount_usdc')} -> {new_payload.get('amount_usdc')}, "
                f"size {old.get('size_shares')} -> {new_payload.get('size_shares')}"
            ),
        )
    if old.get("reason") != new_payload.get("reason"):
        return ReplayDiff(
            record=record,
            new_decision_payload=new_payload,
            kind=ReplayDiffKind.REASON_CHANGED,
            detail=f"{old.get('reason')!r} -> {new_payload.get('reason')!r}",
        )
    return ReplayDiff(record=record, new_decision_payload=new_payload, kind=ReplayDiffKind.OTHER)
