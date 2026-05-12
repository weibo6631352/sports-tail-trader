"""Series 评估器：record-only 骨架。

Worktree 2 阶段：所有子类型都返回带 ``*_MODEL_PENDING`` reject_reason 的
``SeriesEvaluation``（record-only），让 framework 看到 series family 已纳入
诊断、有可审计拒绝原因、但不会构造 BUY。

实际定价模型在 Worktree 3-4 接入：那时 evaluator 会先读 ``SeriesState`` + 第三方
赔率 / 系列赛胜率模型，按子类型分别走 fair_value 计算 + edge / 流动性 / horizon
等闸门，与 outright/evaluator 同构。
"""

from __future__ import annotations

from typing import Any, Mapping

from strategies.current.series.classifier import classify_series_sub_type
from strategies.current.series.types import (
    SeriesCandidate,
    SeriesEvaluation,
    SeriesRejectReason,
    SeriesSubType,
)


_SUBTYPE_PENDING_REASON: dict[SeriesSubType, SeriesRejectReason] = {
    SeriesSubType.WINNER: SeriesRejectReason.WINNER_MODEL_PENDING,
    SeriesSubType.TOTAL_GAMES: SeriesRejectReason.TOTAL_GAMES_MODEL_PENDING,
    SeriesSubType.GAME_HANDICAP: SeriesRejectReason.HANDICAP_MODEL_PENDING,
}


def evaluate_series_opportunity(candidate: SeriesCandidate) -> SeriesEvaluation:
    """评估一个 series outcome 是否值得入场。

    当前 record-only：分类后按子类型返回对应 ``*_MODEL_PENDING`` 拒绝原因。
    OTHER 子类型说明文本未能分类到任一系列赛盘口，记 ``SUBTYPE_UNCLASSIFIED``
    供 audit 调查上游 family 归属是否误判。
    """

    sub_type = classify_series_sub_type(candidate.market)
    if sub_type == SeriesSubType.OTHER:
        return _reject(
            candidate,
            sub_type,
            SeriesRejectReason.SUBTYPE_UNCLASSIFIED,
        )
    return _reject(
        candidate,
        sub_type,
        _SUBTYPE_PENDING_REASON[sub_type],
    )


def _reject(
    candidate: SeriesCandidate,
    sub_type: SeriesSubType,
    reason: SeriesRejectReason,
    *,
    extra_metadata: Mapping[str, Any] | None = None,
) -> SeriesEvaluation:
    metadata: dict[str, Any] = {
        "sub_type": sub_type.value,
        "reject_reason": reason.value,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return SeriesEvaluation(
        accepted=False,
        sub_type=sub_type,
        candidate=candidate,
        reject_reason=reason,
        metadata=metadata,
    )


__all__ = ["evaluate_series_opportunity"]
