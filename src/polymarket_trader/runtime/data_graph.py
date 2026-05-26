"""跨 store 一致视图聚合器 —— `DataGraph`。

从 4 个扁平 store（market_registry / orderbook_history_buffer /
account_state_store / market_metadata_store）按需聚合 `EventView` / `MarketView`
/ `OutcomeView`，供决策层 / aggregator / API 一次性拿到层次化视图。

# 设计约束（见 原架构方案 §3.1 + §11.3）

- **snapshot-and-release**：每个 store 短锁拿引用 → 放锁 → 构造 frozen view。
  P0 路径不持长锁。`account_state_store.snapshot()` 已是 lock-free CoW；
  `market_registry.get_by_*` 内部各自加锁；`orderbook_history_buffer.latest()`
  无锁（CPython deque atomic）；`market_metadata_store.find()` 用 `threading.Lock`。
- **无长期缓存**：每次调用即时构造。view 是即时快照对象，调用方持有期间数据可能
  已更新，但 P0 决策上下文只需要"启动时刻的一致截面"。
- **多 store 间最终一致**：4 个 store 独立锁，构造 view 期间一个 store 已写但另
  一个未写的微弱不一致可能存在（μs 级窗口）；决策器视为正常，下次 tick 自然
  收敛。绝不在 view 构造侧加跨 store 协调锁，避免污染各 store 写路径。

# 关于 OrderbookSnapshot

完整 `OrderbookSnapshot` 由 `OrderbookHistoryBuffer` 持有（环形 deque，按时间窗
裁剪）。`OrderbookDeltaStore` 只存派生的 `OrderbookSample`（方向信号用），不是
完整盘口。本聚合器从 history buffer 取最新 snapshot。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polymarket_trader.domain.graph import EventView, MarketView, OutcomeView

if TYPE_CHECKING:
    from polymarket_trader.runtime.account_state import AccountStateStore
    from polymarket_trader.runtime.market_metadata import MarketMetadataStore
    from polymarket_trader.runtime.orderbook_history_buffer import OrderbookHistoryBuffer
    from polymarket_trader.runtime.registry import MarketRegistry


class DataGraph:
    """4 个扁平 store 的层次化只读视图入口。"""

    def __init__(
        self,
        *,
        market_registry: "MarketRegistry",
        orderbook_history_buffer: "OrderbookHistoryBuffer",
        account_state_store: "AccountStateStore",
        market_metadata_store: "MarketMetadataStore",
    ) -> None:
        self._registry = market_registry
        self._orderbook = orderbook_history_buffer
        self._account = account_state_store
        self._metadata = market_metadata_store

    def market_view(self, condition_id: str) -> MarketView | None:
        market = self._registry.get_by_condition_id(condition_id)
        if market is None:
            return None
        return self._build_market_view(market)

    def outcome_view(self, token_id: str) -> OutcomeView | None:
        market = self._registry.get_by_token_id(token_id)
        if market is None:
            return None
        outcome_meta = market.get_outcome_by_token_id(token_id)
        if outcome_meta is None:
            return None
        account_snapshot = self._account.snapshot()
        return OutcomeView(
            token_id=token_id,
            outcome=outcome_meta.outcome,
            orderbook=self._orderbook.latest(token_id),
            position=account_snapshot.get_position(market.condition_id, token_id),
            open_orders=account_snapshot.open_orders_for_market(market.condition_id, token_id),
        )

    def market_view_for_token(self, token_id: str) -> MarketView | None:
        """从 token_id 反查 market 并返回完整 MarketView——便利方法。

        DecisionContextBuilder 等只有 token_id 时用，避免调用方自己 registry
        反查 cid 再 market_view。
        """

        market = self._registry.get_by_token_id(token_id)
        if market is None:
            return None
        return self._build_market_view(market)

    def event_view(self, event_slug: str) -> EventView | None:
        markets = self._markets_for_event(event_slug)
        if not markets:
            return None
        views = tuple(self._build_market_view(m) for m in markets)
        return EventView(event_slug=event_slug, markets=views)

    def market_views_for_event(self, event_slug: str) -> tuple[MarketView, ...]:
        markets = self._markets_for_event(event_slug)
        return tuple(self._build_market_view(m) for m in markets)

    def all_market_views(self) -> tuple[MarketView, ...]:
        snapshot = self._registry.snapshot()
        return tuple(self._build_market_view(market) for market in snapshot.markets)

    def _build_market_view(self, market) -> MarketView:
        """从 Market 出发，拼齐 outcomes / metadata / pause。

        AccountStateStore 只调一次 snapshot()，所有 outcome 复用同一截面——
        减少 store 入口调用次数，且保证单个 MarketView 内 outcomes 之间一致。
        """

        account_snapshot = self._account.snapshot()
        outcomes = tuple(
            OutcomeView(
                token_id=outcome_meta.token_id,
                outcome=outcome_meta.outcome,
                orderbook=self._orderbook.latest(outcome_meta.token_id),
                position=account_snapshot.get_position(market.condition_id, outcome_meta.token_id),
                open_orders=account_snapshot.open_orders_for_market(
                    market.condition_id, outcome_meta.token_id
                ),
            )
            for outcome_meta in market.outcomes
        )
        return MarketView(
            condition_id=market.condition_id,
            market=market,
            outcomes=outcomes,
            metadata=self._metadata.find(
                condition_id=market.condition_id,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
            ),
            pause=account_snapshot.pause_for_market(market.condition_id),
        )

    def _markets_for_event(self, event_slug: str) -> tuple:
        snapshot = self._registry.snapshot()
        return tuple(m for m in snapshot.markets if m.event_slug == event_slug)
