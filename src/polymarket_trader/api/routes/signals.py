"""信号融合快照 endpoint —— R9 (CPO Round 6)。

暴露 SignalSnapshotStore 的 per-token 最新 LiveSignalSnapshot。前端 PositionAnalysisCard
按 token_id 拉这个端点显示"信号融合"panel。

设计原则（CPO Round 6 强约束）：
- 未重启时优雅降级——store 空时返 ``{snapshot: null, reason: "no_signal_yet"}``，
  **不 500**，前端能渲染占位文案
- 不写新 aggregator class——20 行 inline 读取就够（R5 already 因 over-abstract 多了几个空 class）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from polymarket_trader.api.deps import get_runtime

# R19 revert：routes 注解层批量类型化引入 77 ruff F821 errors；signals.py 的
# RuntimeComponents 字符串 hint 被 revert，restore typing.Any。R20 重做时
# 必须用 PEP 563 + ruff per-file config 排除 F821 误报。

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/snapshot/{token_id}")
async def get_signal_snapshot(
    token_id: str,
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """取某 token 的最新 LiveSignalSnapshot（R6 ring buffer 输出）。

    返回结构：
    - 有数据：``{snapshot: {...11 字段...}}`` —— 字段全在 R6 `LiveSignalSnapshot`
      dataclass 里
    - 无数据：``{snapshot: null, reason: "no_signal_yet"}``——可能是 (a) R6 wiring
      未生效（运行旧 binary，查 `/runtime/build-signature` 确认）；(b) 该 token 还
      未触发任何决策（未进入 size_entry）
    """
    # R18 (Code Q1): 走 RuntimeComponents 强类型直接访问；字段已是 SignalSnapshotStore
    # 非 Optional，重命名/移除会 IDE 报错（不再 getattr 静默断裂）。
    store = runtime.signal_snapshot_store

    snap = store.latest_for_token(token_id)
    if snap is None:
        return {"snapshot": None, "reason": "no_signal_yet"}

    return {
        "snapshot": {
            "condition_id": snap.condition_id,
            "token_id": snap.token_id,
            "pm_best_ask": str(snap.pm_best_ask) if snap.pm_best_ask is not None else None,
            "pm_best_bid": str(snap.pm_best_bid) if snap.pm_best_bid is not None else None,
            "goalserve_fair_prob": (
                str(snap.goalserve_fair_prob) if snap.goalserve_fair_prob is not None else None
            ),
            "math_prob": str(snap.math_prob) if snap.math_prob is not None else None,
            "microprice": str(snap.microprice) if snap.microprice is not None else None,
            "final_prob_p": str(snap.final_prob_p),
            "source_used": snap.source_used,
            "edge_pp": str(snap.edge_pp) if snap.edge_pp is not None else None,
            "timestamp": snap.timestamp.isoformat(),
        },
    }


__all__ = ("router",)
