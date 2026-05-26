"""DecisionContextAggregator —— 单 condition 完整决策上下文一次拉齐。

agent 复盘 / 决策审查 / 前端复盘页 / quant_decider 审查共用同一份聚合数据，
避免连发 5+ 次 endpoint 拼一个上下文（market + position + orderbook
+ live_state + recent audit events）。

# 设计

- 5 个 in-memory 视图全部从 ``data_graph`` + ``market_ws_worker`` + ``market_metadata_store`` 即时聚合（零外部 IO，零 DB）
- 1 次 DB 查询取 recent audit events（命中 ``ix_audit_events_condition_id``
  + ``ix_audit_events_event_title``）

# 输出形态

``GET /decision-context/{condition_id}`` 默认返回所有 outcomes 的视图；
``?token_id=`` 时仅返回该 outcome（其余字段相同）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from polymarket_trader.api.aggregators.market_detail_aggregator import MarketDetailAggregator
from polymarket_trader.api.aggregators.position_aggregator import PositionAggregator
from polymarket_trader.api.aggregators.timeline_aggregator import TimelineAggregator
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.serialization import jsonable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


# 复盘 §15 默认 channel 集，和 routes/audit_events.py by-condition 保持一致
DEFAULT_AUDIT_CHANNELS: tuple[str, ...] = (
    "order_created",
    "allocation_decision_recorded",
    "sports_live_state_recorded",
    "order_matched",
    "order_rejected",
    "fill_recorded",
    "risk_rejection_recorded",
)


class DecisionContextAggregator:
    def __init__(
        self,
        *,
        runtime: Any,
        session_factory: "async_sessionmaker[AsyncSession] | None",
    ) -> None:
        self._runtime = runtime
        self._session_factory = session_factory

    async def get_context(
        self,
        *,
        condition_id: str,
        token_id: str | None = None,
        audit_channels: tuple[str, ...] = DEFAULT_AUDIT_CHANNELS,
        audit_per_channel_limit: int = 20,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any] | None:
        runtime = self._runtime
        if runtime is None or runtime.data_graph is None:
            return None

        # ---------- market view（含 outcomes / orderbook / classification）----------
        market_detail = MarketDetailAggregator(data_graph=runtime.data_graph)
        market_payload = market_detail.detail(condition_id, level="detail")
        if market_payload is None:
            return None

        # ---------- positions（按 token_id 单 outcome 或全部 outcomes）----------
        position_agg = PositionAggregator(data_graph=runtime.data_graph)
        market_view = runtime.data_graph.market_view(condition_id)
        position_items: list[dict[str, Any]] = []
        if market_view is not None:
            for outcome in market_view.outcomes:
                if token_id is not None and outcome.token_id != token_id:
                    continue
                payload = position_agg.position_detail(
                    condition_id, outcome.token_id, level="detail"
                )
                if payload is not None:
                    position_items.append(payload)

        # ---------- live_state（goalserve inplay / livescore record）----------
        live_state: dict[str, Any] | None = None
        meta_store = getattr(runtime, "market_metadata_store", None)
        if meta_store is not None and market_view is not None:
            for rec in meta_store.records():
                if rec.condition_id != condition_id:
                    continue
                live_state = {
                    "source": rec.source,
                    "phase": rec.live_state_phase,
                    "signal_allowed": rec.live_state_signal_allowed,
                    "updated_at": jsonable(rec.updated_at),
                    "payload": dict(rec.live_state_payload or {}),
                }
                break

        # ---------- recent audit events（DB,1 次查询多 channel）----------
        timeline_agg = TimelineAggregator(
            session_factory=self._session_factory, runtime=runtime
        )
        audit = await timeline_agg.get_audit_events_by_condition(
            condition_id=condition_id,
            channels=audit_channels,
            time_range=time_range,
            per_channel_limit=audit_per_channel_limit,
        )

        return {
            "condition_id": condition_id,
            "token_id": token_id,
            "market": market_payload,
            "positions": position_items,
            "live_state": live_state,
            "recent_audit_events": audit,
        }


__all__ = ["DecisionContextAggregator", "DEFAULT_AUDIT_CHANNELS"]
