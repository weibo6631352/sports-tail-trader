"""每市场配套的可变运行时 state(audit dedupe + 节流计时器)。

生命周期严格绑定 ``MarketRegistry`` 持有的 Market:
- ``upsert(market)`` 时若 cid 首次出现,registry 自动创建 companion;
- ``remove_market(cid)`` 时 registry 同步 pop companion → state 自然消失,
  无需各 worker 手动维护"按 cid 索引"的 dict + prune 联动。

不放在 ``domain.Market`` 里的理由(CLAUDE.md §3 Domain 纯度):
- ``Market`` 是 ``@dataclass(frozen=True)`` 纯领域对象,不接受 mutable state;
- companion 含运行时纯应用层的去重/计时器,与领域无关;
- 通过 registry 中介访问保证 lifecycle 一致(market 死 → companion 死)。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MarketCompanionState:
    """每市场的 audit dedupe + 节流计时器(全部 mutable 可变).

    使用方:
    - reconcile worker: already_paused_audit / last_reconcile_applied_at / last_reconcile_diff_at
    - sports_live_state worker: last_audit_state_hash / last_audit_emit_at
    - market_ingest_service(discovery): last_filter_reason / last_filter_emit_at

    一律按 monotonic 时间戳(time.monotonic()),不混 wall clock.
    """

    # reconcile worker:状态去重 + 节流
    already_paused_audit: bool = False
    last_reconcile_applied_at_mono: float = 0.0
    last_reconcile_diff_at_mono: float = 0.0

    # sports_live_state worker:hash dedupe + 30s 兜底节流
    last_audit_state_hash: str | None = None
    last_audit_emit_at_mono: float = 0.0

    # market_ingest_service(discovery filter):reason dedupe + 60s 兜底节流
    last_filter_reason: str | None = None
    last_filter_emit_at_mono: float = 0.0

    # sports_live_state worker:terminal pause 已发标记(避免重复 pause)
    terminal_pause_emitted: bool = False
    # sports_live_state worker:missing-data gap audit 按分钟去重
    last_gap_recorded_minute: int | None = None


__all__ = ["MarketCompanionState"]
