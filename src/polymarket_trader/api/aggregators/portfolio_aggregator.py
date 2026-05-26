"""PortfolioAggregator —— 基于 DataGraph 的组合暴露聚合（示例 aggregator）。

# 用法

```python
from polymarket_trader.api.aggregators import PortfolioAggregator

agg = PortfolioAggregator(data_graph=runtime.data_graph)
summary = agg.exposure(level="summary")     # 仅总览（10 字段）
detail  = agg.exposure(level="detail")      # 完整 per-market 数据
```

# 设计

按 §12.3 ② 字段选择 + ① 统一聚合：
- summary 默认（避免无脑拉详情）
- detail 含每个 MarketView 的完整 metadata + outcomes 列表
- 只读 DataGraph，不调外部 API / 不写 store / 不发 audit

# 设计

走 DataGraph 统一 view，消除跨 store 拼接（registry + account_state +
market_metadata）——同一份 MarketView 同时承载 metadata + 仓位 + 盘口快照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from polymarket_trader.runtime.data_graph import DataGraph

Level = Literal["summary", "detail"]


@dataclass(frozen=True, slots=True)
class PortfolioExposureView:
    """组合暴露聚合 view（API-friendly dict 包装）。"""

    summary: dict[str, Any]
    markets: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class PortfolioAggregator:
    def __init__(self, *, data_graph: "DataGraph") -> None:
        self._graph = data_graph

    def exposure(self, *, level: Level = "summary") -> PortfolioExposureView:
        market_views = self._graph.all_market_views()
        # 只统计有持仓的 market（其他 noise 过滤）
        with_position = tuple(mv for mv in market_views if mv.has_any_position)

        total_position_usdc = sum(
            (mv.total_position_usdc for mv in with_position),
            start=Decimal("0"),
        )
        total_shares = sum(
            (mv.total_shares for mv in with_position),
            start=Decimal("0"),
        )

        summary = {
            "market_count": len(with_position),
            "total_position_usdc": str(total_position_usdc),
            "total_shares": str(total_shares),
            "paused_market_count": sum(1 for mv in with_position if mv.is_paused),
        }
        if level == "summary":
            return PortfolioExposureView(summary=summary)

        markets = tuple(self._serialize_market(mv) for mv in with_position)
        return PortfolioExposureView(summary=summary, markets=markets)

    def _serialize_market(self, mv) -> dict[str, Any]:
        outcomes_payload = tuple(
            {
                "token_id": ov.token_id,
                "outcome": ov.outcome,
                "shares": str(ov.shares),
                "best_bid": str(ov.best_bid) if ov.best_bid is not None else None,
                "best_ask": str(ov.best_ask) if ov.best_ask is not None else None,
                "has_open_buy": ov.has_open_buy,
                "has_open_sell": ov.has_open_sell,
            }
            for ov in mv.outcomes
        )
        return {
            "condition_id": mv.condition_id,
            "market_slug": mv.market_slug,
            "event_slug": mv.event_slug,
            "event_title": mv.event_title,
            "is_paused": mv.is_paused,
            "total_position_usdc": str(mv.total_position_usdc),
            "total_shares": str(mv.total_shares),
            "outcomes": outcomes_payload,
        }
