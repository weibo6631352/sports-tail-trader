"""MarketDetailAggregator —— 基于 DataGraph 的单 market 详情聚合。

按 docs/新架构方案.md §12.3 ⑤。把跨 store（registry / orderbook / metadata /
account）的拼装统一到 MarketView，路由层只调本 aggregator 不再手拼。

# 三层 endpoint 对应

| Endpoint | 方法 | 内容 |
|---|---|---|
| `GET /markets/{cid}?level=summary` | `detail(cid, summary)` | 10 字段总览 |
| `GET /markets/{cid}?level=detail` | `detail(cid, detail)` | 含 outcomes / orderbook / metadata / pause |
| `POST /markets/batch` | `batch_detail(cids, level)` | N 个市场一次返回（§12.3 ⑤批量） |
| `GET /markets/{cid}/orderbook` | `orderbook(cid, token_id)` | 单 outcome 完整盘口 |
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from polymarket_trader.domain.graph import MarketView, OutcomeView
    from polymarket_trader.runtime.data_graph import DataGraph

Level = Literal["summary", "detail"]


class MarketDetailAggregator:
    def __init__(self, *, data_graph: "DataGraph") -> None:
        self._graph = data_graph

    def detail(self, condition_id: str, *, level: Level = "summary") -> dict[str, Any] | None:
        view = self._graph.market_view(condition_id)
        if view is None:
            return None
        return self._serialize(view, level=level)

    def batch_detail(
        self,
        condition_ids: tuple[str, ...],
        *,
        level: Level = "summary",
    ) -> tuple[dict[str, Any], ...]:
        """一次返回 N 个 market 详情，避免 agent 多次 single-detail 调用。"""

        results = []
        for cid in condition_ids:
            payload = self.detail(cid, level=level)
            if payload is not None:
                results.append(payload)
        return tuple(results)

    def orderbook(self, token_id: str) -> dict[str, Any] | None:
        outcome = self._graph.outcome_view(token_id)
        if outcome is None or outcome.orderbook is None:
            return None
        return self._serialize_orderbook(outcome)

    def all_markets(self, *, level: Level = "summary") -> tuple[dict[str, Any], ...]:
        """list_markets 等价——遍历所有 market view 返回 payload tuple。"""

        return tuple(
            self._serialize(view, level=level) for view in self._graph.all_market_views()
        )

    def _serialize(self, view: "MarketView", *, level: Level) -> dict[str, Any]:
        market = view.market
        summary = {
            "condition_id": view.condition_id,
            "market_slug": view.market_slug,
            "event_slug": view.event_slug,
            "event_title": view.event_title,
            "trading_status": market.trading_status.value,
            "is_paused": view.is_paused,
            "has_any_position": view.has_any_position,
            "total_position_usdc": str(view.total_position_usdc),
            "total_shares": str(view.total_shares),
            "outcome_count": len(view.outcomes),
        }
        if level == "summary":
            return summary
        return {
            **summary,
            "market_name": market.market_name,
            "market_question": market.market_question,
            "tick_size": str(market.tick_size),
            "min_order_size": str(market.min_order_size),
            "neg_risk": market.neg_risk,
            "category": market.category,
            "tags": list(market.tags),
            "sports_market_type": market.sports_market_type,
            "matched_keywords": list(market.matched_keywords),
            "game_start_time": (
                market.game_start_time.isoformat() if market.game_start_time else None
            ),
            "end_date": market.end_date.isoformat() if market.end_date else None,
            "pause": (
                {
                    "reason": view.pause.reason,
                    "source": view.pause.source.value,
                    "recoverable": view.pause.recoverable,
                }
                if view.pause is not None
                else None
            ),
            "metadata": (
                view.metadata.as_payload() if view.metadata is not None else None
            ),
            "outcomes": tuple(self._serialize_outcome(ov) for ov in view.outcomes),
        }

    def _serialize_outcome(self, outcome: "OutcomeView") -> dict[str, Any]:
        return {
            "token_id": outcome.token_id,
            "outcome": outcome.outcome,
            "shares": str(outcome.shares),
            "best_bid": str(outcome.best_bid) if outcome.best_bid is not None else None,
            "best_ask": str(outcome.best_ask) if outcome.best_ask is not None else None,
            "has_open_buy": outcome.has_open_buy,
            "has_open_sell": outcome.has_open_sell,
            "open_orders_count": len(outcome.open_orders),
            "position": (
                {
                    "shares": str(outcome.position.shares),
                    "cost_usdc": str(outcome.position.cost_usdc),
                    "cur_price": (
                        str(outcome.position.cur_price)
                        if outcome.position.cur_price is not None
                        else None
                    ),
                    "redeemable": outcome.position.redeemable,
                }
                if outcome.position is not None
                else None
            ),
        }

    def _serialize_orderbook(self, outcome: "OutcomeView") -> dict[str, Any]:
        ob = outcome.orderbook
        if ob is None:
            return {}
        return {
            "token_id": outcome.token_id,
            "outcome": outcome.outcome,
            "best_bid": str(ob.best_bid) if ob.best_bid is not None else None,
            "best_ask": str(ob.best_ask) if ob.best_ask is not None else None,
            "best_bid_size": str(ob.best_bid_size) if ob.best_bid_size is not None else None,
            "best_ask_size": str(ob.best_ask_size) if ob.best_ask_size is not None else None,
            "spread": str(ob.spread) if ob.spread is not None else None,
            "microprice": str(ob.microprice) if ob.microprice is not None else None,
            "bids": tuple({"price": str(lv.price), "size": str(lv.size)} for lv in ob.bids),
            "asks": tuple({"price": str(lv.price), "size": str(lv.size)} for lv in ob.asks),
            "received_at": ob.received_at.isoformat(),
        }
