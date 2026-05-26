"""基于 ``DecisionRecord`` 的决策回放与对比。

工作流：
    1. 实盘录制：framework 把每次 hook 调用以 ``DECISION_RECORDED`` 投到 outbox，
       ``PersistenceWorker`` 落到 ``decision_records`` 表。
    2. 改完一行决策代码后，在 ReplayHarness 上灌入旧录制 + 新 hooks。
    3. ReplayHarness 比对新旧 decision_output，输出每条 record 的 diff 分类。

这是一个**离线复盘工具**，不进 P0 主链路。它故意不重建 ``DecisionContext`` 对象，
而是直接给新 hooks 一个能反映原始 context 的轻量结构——这样开发者
即使改了 Context 的字段顺序也能 replay 历史录制（结构性兼容兜底，避免每改一次
context 就让整个录制库失效）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping

from polymarket_trader.domain.decisions import DecisionRecord


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
    new_decision_output: Mapping[str, Any]
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


# 调用方提供的"基于 record + 新 hooks 重算 decision_output"的回调。
# 让 ReplayHarness 不直接 import workflow 细节——调用方决定如何重建 context。
ReplayDecisionFn = Callable[[DecisionRecord], Mapping[str, Any]]


def replay_records(
    records: Iterable[DecisionRecord],
    *,
    replay_decision: ReplayDecisionFn,
) -> ReplayReport:
    """对每条 record 调一次 ``replay_decision``，与原 ``decision_output`` 对比。"""

    diffs: list[ReplayDiff] = []
    for record in records:
        try:
            new_output = dict(replay_decision(record))
        except Exception as exc:
            new_output = {"_replay_error": str(exc)}
        diffs.append(_classify(record, new_output))
    return ReplayReport(diffs=tuple(diffs))


def _classify(record: DecisionRecord, new_output: Mapping[str, Any]) -> ReplayDiff:
    old = dict(record.decision_output)
    if "_replay_error" in new_output:
        return ReplayDiff(
            record=record,
            new_decision_output=new_output,
            kind=ReplayDiffKind.OTHER,
            detail=str(new_output["_replay_error"]),
        )
    if old == new_output:
        return ReplayDiff(record=record, new_decision_output=new_output, kind=ReplayDiffKind.UNCHANGED)
    if old.get("action") != new_output.get("action"):
        return ReplayDiff(
            record=record,
            new_decision_output=new_output,
            kind=ReplayDiffKind.ACTION_CHANGED,
            detail=f"{old.get('action')} -> {new_output.get('action')}",
        )
    if old.get("price") != new_output.get("price"):
        return ReplayDiff(
            record=record,
            new_decision_output=new_output,
            kind=ReplayDiffKind.PRICE_CHANGED,
            detail=f"{old.get('price')} -> {new_output.get('price')}",
        )
    if old.get("amount_usdc") != new_output.get("amount_usdc") or old.get("size_shares") != new_output.get("size_shares"):
        return ReplayDiff(
            record=record,
            new_decision_output=new_output,
            kind=ReplayDiffKind.AMOUNT_CHANGED,
            detail=(
                f"amount {old.get('amount_usdc')} -> {new_output.get('amount_usdc')}, "
                f"size {old.get('size_shares')} -> {new_output.get('size_shares')}"
            ),
        )
    if old.get("reason") != new_output.get("reason"):
        return ReplayDiff(
            record=record,
            new_decision_output=new_output,
            kind=ReplayDiffKind.REASON_CHANGED,
            detail=f"{old.get('reason')!r} -> {new_output.get('reason')!r}",
        )
    return ReplayDiff(record=record, new_decision_output=new_output, kind=ReplayDiffKind.OTHER)
