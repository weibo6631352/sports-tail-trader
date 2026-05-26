"""SettlementAggregator —— 市场结算历史 + 单市场结算详情（§12.2 审计查询类）。

# Endpoint 对应

| Endpoint | 方法 | 内容 |
|---|---|---|
| `GET /markets/settlements` | `list_settlements(...)` | 市场结算 audit 事件分页 |
| `GET /markets/{cid}/settlement` | `settlement_for(cid)` | 单市场最新结算 + 我们 fair_value 偏差 |

# 设计

list_settlements 本质是 audit_events 按 `MARKET_SETTLED` event_title 过滤——
内部委托 TimelineAggregator.list_audit_events 实现，消除重复 DB 查询代码。
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from polymarket_trader.app.admin_serialization import AdminSerializer
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.time_filters import TimeRange

from ._db import RepositoryGroup, with_repositories
from .timeline_aggregator import TimelineAggregator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SettlementAggregator:
    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession] | None",
        serializer: AdminSerializer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._serializer = serializer or AdminSerializer.from_runtime(None)
        self._timeline = TimelineAggregator(
            session_factory=session_factory, serializer=self._serializer
        )

    async def list_settlements(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """市场结算历史（含 winning_token_id / outcome / 来源）。"""

        return await self._timeline.list_audit_events(
            limit=limit,
            offset=offset,
            event_title=DomainEventType.MARKET_SETTLED.value,
            condition_id=condition_id,
            time_range=time_range,
        )

    async def settlement_for(self, condition_id: str) -> dict[str, Any] | None:
        """单市场最新 settlement + 我们最后一次 fair_value 偏差。

        给"市场结算后我们的定价对不对"快速诊断。无结算返回 None → 404。
        """

        if self._session_factory is None:
            return None

        async def _query(repos: RepositoryGroup) -> tuple[Any, Any]:
            settle_page = await repos.audit.list_audit_events_snapshot(
                limit=1,
                offset=0,
                event_title=DomainEventType.MARKET_SETTLED.value,
                condition_id=condition_id,
            )
            decision_page = await repos.decision.list_decisions_snapshot(
                limit=1,
                offset=0,
                condition_id=condition_id,
                accepted=True,
            )
            return settle_page, decision_page

        settle_page, decision_page = await with_repositories(self._session_factory, _query)
        settle_items = tuple(settle_page.items or ())
        if not settle_items:
            return None
        settlement = settle_items[0]
        payload = settlement.payload if isinstance(settlement.payload, dict) else {}
        winning_token_id_raw = payload.get("winning_token_id")
        winning_token_id = (
            str(winning_token_id_raw) if winning_token_id_raw is not None else None
        )
        last_decision = (
            tuple(decision_page.items or ())[0] if (decision_page.items or ()) else None
        )
        last_fair_value: str | None = None
        last_token_id: str | None = None
        deviation: str | None = None
        if last_decision is not None and isinstance(last_decision.decision_output, dict):
            fv = last_decision.decision_output.get("fair_value")
            last_token_id = last_decision.token_id
            if fv is not None:
                last_fair_value = str(fv)
                try:
                    actual = (
                        Decimal("1")
                        if winning_token_id and last_token_id == winning_token_id
                        else Decimal("0")
                    )
                    fair_dec = Decimal(str(fv))
                    deviation = str(actual - fair_dec)
                except Exception:  # noqa: BLE001
                    deviation = None
        return {
            "condition_id": condition_id,
            "settled_at": payload.get("settled_at"),
            "winning_token_id": winning_token_id,
            "winning_outcome": payload.get("winning_outcome"),
            "source": payload.get("source"),
            "operator": payload.get("operator"),
            "last_decision": {
                "record_id": None if last_decision is None else last_decision.record_id,
                "token_id": last_token_id,
                "fair_value": last_fair_value,
                "outcome_minus_fair": deviation,
            },
        }
