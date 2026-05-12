from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Protocol

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.account import MarketPause
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order, OrderResult
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api.decisions import EntryCandidate, MarketTokenView
from polymarket_trader.extension_api.manual_confirmation import ManualConfirmation


class AccountSnapshotView(Protocol):
    @property
    def balance_usdc(self) -> Decimal: ...

    @property
    def allowance_usdc(self) -> Decimal: ...

    @property
    def available_usdc(self) -> Decimal: ...

    @property
    def positions(self) -> tuple[Position, ...]: ...

    @property
    def open_orders(self) -> tuple[Order, ...]: ...

    @property
    def allow_new_entries(self) -> bool: ...

    @property
    def market_pauses(self) -> tuple[MarketPause, ...]: ...

    @property
    def last_reconcile_at(self) -> datetime | None: ...

    def get_position(self, condition_id: str, token_id: str) -> Position | None: ...

    def is_market_paused(self, condition_id: str) -> bool: ...

    def open_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]: ...

    def open_buy_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]: ...

    def open_sell_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]: ...


@dataclass(frozen=True, slots=True)
class MarketView:
    market: Market | None = None
    token_id: str | None = None
    orderbook: OrderbookSnapshot | None = None
    market_token_views: tuple[MarketTokenView, ...] = ()


@dataclass(frozen=True, slots=True)
class AccountView:
    snapshot: AccountSnapshotView | None = None
    position: Position | None = None
    open_orders: tuple[Order, ...] = ()


@dataclass(frozen=True, slots=True)
class BudgetView:
    """传给策略 hook 的资金视图。

    ``bankroll_usdc`` = ``min(account.available_usdc, settings.portfolio_budget_usdc)``，
    Kelly 公式直接吃这个值。``portfolio_budget_usdc`` / ``available_usdc`` 仅作上下文展示，
    策略不应再用它们做仓位决策——所有 sizing 走 ``kelly_*`` 字段。
    """

    portfolio_budget_usdc: Decimal | None = None
    available_usdc: Decimal | None = None
    bankroll_usdc: Decimal | None = None
    kelly_fraction: Decimal | None = None
    kelly_max_position_fraction: Decimal | None = None
    kelly_min_edge: Decimal | None = None
    kelly_min_stake_usdc: Decimal | None = None
    kelly_allow_round_up_to_market_min: bool | None = None
    kelly_round_up_max_overbet_ratio: Decimal | None = None
    kelly_drawdown_halt_fraction: Decimal | None = None
    peak_bankroll_usdc: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SizingView:
    allocation_plan: AllocationPlan | None = None
    allocation: Allocation | None = None
    amount_usdc: Decimal | None = None
    size_shares: Decimal | None = None
    entry_candidates: tuple[EntryCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtensionContext:
    """框架向策略 hook 输入的上下文。

    顶层字段保持向前兼容：策略既可以读 ``context.market`` 也可以读
    ``context.market_view.market``；framework 内部新代码推荐使用子视图。
    """

    trace_id: str
    # strategy_id 是框架/策略契约的强字段——所有 hook 调用必须显式提供。
    # 框架内部禁止填默认值；策略 manifest/spec 提供单一来源（CLAUDE.md §10）。
    strategy_id: str
    market: Market | None = None
    token_id: str | None = None
    orderbook: OrderbookSnapshot | None = None
    market_token_views: tuple[MarketTokenView, ...] = ()
    account_snapshot: AccountSnapshotView | None = None
    position: Position | None = None
    open_orders: tuple[Order, ...] = ()
    entry_candidates: tuple[EntryCandidate, ...] = ()
    order_result: OrderResult | None = None
    now: datetime | None = None
    portfolio_budget_usdc: Decimal | None = None
    available_usdc: Decimal | None = None
    bankroll_usdc: Decimal | None = None
    kelly_fraction: Decimal | None = None
    kelly_max_position_fraction: Decimal | None = None
    kelly_min_edge: Decimal | None = None
    kelly_min_stake_usdc: Decimal | None = None
    kelly_allow_round_up_to_market_min: bool | None = None
    kelly_round_up_max_overbet_ratio: Decimal | None = None
    kelly_drawdown_halt_fraction: Decimal | None = None
    peak_bankroll_usdc: Decimal | None = None
    allocation_plan: AllocationPlan | None = None
    allocation: Allocation | None = None
    amount_usdc: Decimal | None = None
    size_shares: Decimal | None = None
    manual_confirmation: ManualConfirmation | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def market_view(self) -> MarketView:
        return MarketView(
            market=self.market,
            token_id=self.token_id,
            orderbook=self.orderbook,
            market_token_views=self.market_token_views,
        )

    @property
    def account_view(self) -> AccountView:
        return AccountView(
            snapshot=self.account_snapshot,
            position=self.position,
            open_orders=self.open_orders,
        )

    @property
    def budget_view(self) -> BudgetView:
        return BudgetView(
            portfolio_budget_usdc=self.portfolio_budget_usdc,
            available_usdc=self.available_usdc,
            bankroll_usdc=self.bankroll_usdc,
            kelly_fraction=self.kelly_fraction,
            kelly_max_position_fraction=self.kelly_max_position_fraction,
            kelly_min_edge=self.kelly_min_edge,
            kelly_min_stake_usdc=self.kelly_min_stake_usdc,
            kelly_allow_round_up_to_market_min=self.kelly_allow_round_up_to_market_min,
            kelly_round_up_max_overbet_ratio=self.kelly_round_up_max_overbet_ratio,
            kelly_drawdown_halt_fraction=self.kelly_drawdown_halt_fraction,
            peak_bankroll_usdc=self.peak_bankroll_usdc,
        )

    @property
    def sizing_view(self) -> SizingView:
        return SizingView(
            allocation_plan=self.allocation_plan,
            allocation=self.allocation,
            amount_usdc=self.amount_usdc,
            size_shares=self.size_shares,
            entry_candidates=self.entry_candidates,
        )
