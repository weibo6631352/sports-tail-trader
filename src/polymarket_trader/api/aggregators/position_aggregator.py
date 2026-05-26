"""PositionAggregator —— 基于 DataGraph 的持仓聚合。

直接通过 DataGraph.all_market_views 走 OutcomeView——持仓自动带上 market
metadata（market_slug / event_title / outcome name）+ 当前盘口 best_bid/ask，
省一次 join。比直接读 AccountStateStore 多两个维度。

# Endpoint 对应

| Endpoint | 方法 | 内容 |
|---|---|---|
| `GET /positions` | `list_positions(level=summary or detail, only_with_shares=True)` | 持仓列表 |
| `GET /positions/{cid}/{tid}` | `position_detail(cid, tid)` | 单 outcome 详情 |
| `POST /positions/batch` | `batch_positions(specs)` | N 条 (cid, tid) 一次返回 |
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable, Literal

if TYPE_CHECKING:
    from polymarket_trader.domain.graph import OutcomeView
    from polymarket_trader.runtime.data_graph import DataGraph

Level = Literal["summary", "detail"]


class PositionAggregator:
    def __init__(self, *, data_graph: "DataGraph") -> None:
        self._graph = data_graph

    def list_positions(
        self,
        *,
        level: Level = "summary",
        only_with_shares: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        results = []
        for mv in self._graph.all_market_views():
            for ov in mv.outcomes:
                if only_with_shares and (ov.position is None or ov.position.shares <= Decimal("0")):
                    continue
                if ov.position is None:
                    continue
                results.append(self._serialize(mv, ov, level=level))
        # 默认按持仓金额降序——大头放前面（CLAUDE.md §17 不丢机会原则）
        results.sort(
            key=lambda r: Decimal(r.get("position_usdc", "0")),
            reverse=True,
        )
        return tuple(results)

    def position_detail(
        self,
        condition_id: str,
        token_id: str,
        *,
        level: Level = "detail",
    ) -> dict[str, Any] | None:
        mv = self._graph.market_view(condition_id)
        if mv is None:
            return None
        ov = mv.outcome_for(token_id)
        if ov is None or ov.position is None:
            return None
        return self._serialize(mv, ov, level=level)

    def batch_positions(
        self,
        specs: Iterable[tuple[str, str]],
        *,
        level: Level = "summary",
    ) -> tuple[dict[str, Any], ...]:
        results = []
        for cid, tid in specs:
            payload = self.position_detail(cid, tid, level=level)
            if payload is not None:
                results.append(payload)
        return tuple(results)

    def _serialize(self, mv, ov: "OutcomeView", *, level: Level) -> dict[str, Any]:
        pos = ov.position
        assert pos is not None  # caller 已 filter

        # MTM: cur_price 优先，否则 best_bid 兜底
        mtm_price = pos.cur_price if pos.cur_price is not None else ov.best_bid
        position_usdc = pos.shares * mtm_price if mtm_price is not None else Decimal("0")

        summary = {
            "condition_id": mv.condition_id,
            "token_id": ov.token_id,
            "outcome": ov.outcome,
            "market_slug": mv.market_slug,
            "event_title": mv.event_title,
            "shares": str(pos.shares),
            "cost_usdc": str(pos.cost_usdc),
            "cur_price": str(pos.cur_price) if pos.cur_price is not None else None,
            "position_usdc": str(position_usdc),
            "best_bid": str(ov.best_bid) if ov.best_bid is not None else None,
            "best_ask": str(ov.best_ask) if ov.best_ask is not None else None,
            "has_open_buy": ov.has_open_buy,
            "has_open_sell": ov.has_open_sell,
            "redeemable": pos.redeemable,
            "settled_zero_value": pos.settled_zero_value,
            "is_paused": mv.is_paused,
        }
        if level == "summary":
            return summary
        return {
            **summary,
            "open_buy_shares": str(pos.open_buy_shares),
            "open_sell_shares": str(pos.open_sell_shares),
            "pending_buy_shares": str(pos.pending_buy_shares),
            "confirmed_shares": str(pos.confirmed_shares),
            "cash_pnl": str(pos.cash_pnl) if pos.cash_pnl is not None else None,
            "percent_pnl": str(pos.percent_pnl) if pos.percent_pnl is not None else None,
            "realized_pnl": str(pos.realized_pnl) if pos.realized_pnl is not None else None,
            "last_order_id": pos.last_order_id,
            "last_trade_id": pos.last_trade_id,
            "confirmation_status": pos.confirmation_status,
            "updated_at": pos.updated_at.isoformat() if pos.updated_at else None,
        }
