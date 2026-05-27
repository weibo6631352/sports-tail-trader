"""市场入场前可补充的运行时 metadata DTO。

`EntryMetadataRecord` 是纯数据载体，无 runtime / IO 依赖。`MarketMetadataStore`
（持有此 DTO 的容器）仍在 `runtime/market_metadata.py`；本模块只承载 DTO 定义。

`DataGraph` 构造 `MarketView` 时引用此 DTO 作为 `MarketView.metadata` 字段类型，
domain 层不需要再依赖 runtime——视图层因此保持纯净。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from polymarket_trader.serialization import jsonable

# Goalserve 把已结算事件移出 inplay feed 后,本端 last live_state_payload 冻结;
# observed_at 超过该秒数 → 判定 stale (≈"等结算窗口")。5min 是经验值:
# Polymarket 结算窗口 10-30min,5min stale 比 30min 更早提示操盘。
_AWAITING_SETTLEMENT_STALE_SECONDS = 300
_AWAITING_SETTLEMENT_TERMINAL_STATUSES = frozenset(
    {"ended", "cancelled", "retired", "postponed", "disputed", "finished", "ft"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class EntryMetadataRecord:
    """入场判断前可补充的运行时 metadata 快照。

    `metadata` 是workflow 可透传的自由 dict（operator 详情透传用）；framework 决策只读
    强类型 `live_state_*` 字段，避免依赖 workflow 私有 metadata key。
    """

    condition_id: str | None = None
    market_slug: str | None = None
    event_slug: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source: str = "manual"
    updated_at: datetime = field(default_factory=_utc_now)
    live_state_signal_allowed: bool | None = None
    live_state_signal_reason: str = ""
    live_state_phase: str = ""
    live_state_payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def live_state_status(self) -> str:
        return str((self.live_state_payload or {}).get("status") or "").lower()

    def is_awaiting_settlement(self, *, now: datetime | None = None) -> bool:
        """已结算窗口判定: live_state 显式终态 OR Goalserve 停推 >5min。

        排除条件: live_state_payload 完全为空(本就没匹配上直播源)不算 awaiting,
        让上层走 missing_live_game_state 路径。
        """
        if not self.live_state_payload:
            return False
        if self.live_state_status in _AWAITING_SETTLEMENT_TERMINAL_STATUSES:
            return True
        ref = now or _utc_now()
        return (ref - self.updated_at).total_seconds() > _AWAITING_SETTLEMENT_STALE_SECONDS

    def as_payload(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "market_slug": self.market_slug,
            "event_slug": self.event_slug,
            "metadata": jsonable(self.metadata),
            "source": self.source,
            "updated_at": self.updated_at.isoformat(),
            "live_state_signal_allowed": self.live_state_signal_allowed,
            "live_state_signal_reason": self.live_state_signal_reason,
            "live_state_phase": self.live_state_phase,
            "live_state_payload": jsonable(self.live_state_payload),
        }
