from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api.summary import StrategySummary


class ExtensionAction(StrEnum):
    SKIP = "skip"
    BUY = "buy"
    SELL = "sell"
    CANCEL = "cancel"
    REPLACE = "replace"


class QuantTriggerKind(StrEnum):
    """量化决策器触发源。

    真正量化形态：仅两类触发——
    - market_ws book / price_change（盘口变化，可能 BUY / SELL / replace）
    - reconcile 周期（兜底 + 清理僵尸订单 + pause 信号）

    user_ws fill / order 事件不触发量化器：fill 是过去决策的结果，下次 market
    tick 时量化器自然读最新 AccountSnapshot 决策。
    """

    # market_ws book + price_change 合并：盘口变化（成交 / 挂单 / 撤单）
    MARKET_TICK = "market_tick"
    # reconcile 周期（默认 40s）：用来清理僵尸订单 / 覆盖裸持仓 / 必要时 pause。
    RECONCILE_CYCLE = "reconcile_cycle"


class DecisionKind(StrEnum):
    """策略决策的语义分类。framework 用 decision_kind 而不是 metadata 字符串
    判断"该决策是入场 / 加仓 / 退场 / 跟单 / 恢复"。策略下决策时必须显式声明。"""

    ENTRY = "entry"
    SCALE_IN = "scale_in"
    EXIT = "exit"
    FOLLOW_UP = "follow_up"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class MarketTokenView:
    token_id: str
    outcome: str
    orderbook: OrderbookSnapshot | None = None
    position: Position | None = None
    open_orders: tuple[Order, ...] = ()


@dataclass(frozen=True, slots=True)
class UniverseDecision:
    selected: bool
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def include(
        cls,
        *,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "UniverseDecision":
        return cls(selected=True, reason=reason, metadata=metadata or {})

    @classmethod
    def exclude(
        cls,
        *,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> "UniverseDecision":
        return cls(selected=False, reason=reason, metadata=metadata or {})


@dataclass(frozen=True, slots=True)
class EntryCandidate:
    market: Market
    token_id: str
    orderbook: OrderbookSnapshot
    position: Position | None = None
    open_orders: tuple[Order, ...] = ()
    idempotency_key: str | None = None

    @property
    def condition_id(self) -> str:
        return self.market.condition_id


@dataclass(frozen=True, slots=True)
class ExtensionDecision:
    action: ExtensionAction
    reason: str = ""
    token_id: str | None = None
    price: Decimal | None = None
    amount_usdc: Decimal | None = None
    size_shares: Decimal | None = None
    order_id: str | None = None
    order_type: OrderType | None = None
    post_only: bool = False
    market_slug: str | None = None
    decision_kind: DecisionKind | None = None
    intent_tags: frozenset[str] = field(default_factory=frozenset)
    summary: StrategySummary | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def skip(
        cls,
        *,
        reason: str,
        decision_kind: DecisionKind | None = None,
        intent_tags: frozenset[str] | None = None,
        summary: StrategySummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(
            action=ExtensionAction.SKIP,
            reason=reason,
            decision_kind=decision_kind,
            intent_tags=intent_tags or frozenset(),
            summary=summary,
            metadata=metadata or {},
        )

    @classmethod
    def buy(
        cls,
        *,
        reason: str,
        token_id: str | None,
        price: Decimal,
        amount_usdc: Decimal,
        order_type: OrderType | None = None,
        post_only: bool = False,
        market_slug: str | None = None,
        decision_kind: DecisionKind | None = None,
        intent_tags: frozenset[str] | None = None,
        summary: StrategySummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(
            action=ExtensionAction.BUY,
            reason=reason,
            token_id=token_id,
            price=price,
            amount_usdc=amount_usdc,
            order_type=order_type,
            post_only=post_only,
            market_slug=market_slug,
            decision_kind=decision_kind,
            intent_tags=intent_tags or frozenset(),
            summary=summary,
            metadata=metadata or {},
        )

    @classmethod
    def sell(
        cls,
        *,
        reason: str,
        token_id: str | None,
        price: Decimal,
        size_shares: Decimal,
        order_type: OrderType | None = None,
        post_only: bool = False,
        market_slug: str | None = None,
        decision_kind: DecisionKind | None = None,
        intent_tags: frozenset[str] | None = None,
        summary: StrategySummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(
            action=ExtensionAction.SELL,
            reason=reason,
            token_id=token_id,
            price=price,
            size_shares=size_shares,
            order_type=order_type,
            post_only=post_only,
            market_slug=market_slug,
            decision_kind=decision_kind,
            intent_tags=intent_tags or frozenset(),
            summary=summary,
            metadata=metadata or {},
        )

    @classmethod
    def cancel(
        cls,
        *,
        reason: str,
        token_id: str | None,
        order_id: str,
        market_slug: str | None = None,
        decision_kind: DecisionKind | None = None,
        intent_tags: frozenset[str] | None = None,
        summary: StrategySummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(
            action=ExtensionAction.CANCEL,
            reason=reason,
            token_id=token_id,
            order_id=order_id,
            market_slug=market_slug,
            decision_kind=decision_kind,
            intent_tags=intent_tags or frozenset(),
            summary=summary,
            metadata=metadata or {},
        )

    @classmethod
    def replace(
        cls,
        *,
        reason: str,
        token_id: str | None,
        order_id: str,
        price: Decimal,
        size_shares: Decimal,
        market_slug: str | None = None,
        decision_kind: DecisionKind | None = None,
        intent_tags: frozenset[str] | None = None,
        summary: StrategySummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExtensionDecision":
        return cls(
            action=ExtensionAction.REPLACE,
            reason=reason,
            token_id=token_id,
            order_id=order_id,
            price=price,
            size_shares=size_shares,
            market_slug=market_slug,
            decision_kind=decision_kind,
            intent_tags=intent_tags or frozenset(),
            summary=summary,
            metadata=metadata or {},
        )


@dataclass(frozen=True, slots=True)
class EntrySizing:
    allocation_plan: AllocationPlan
    allocation: Allocation | None = None
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def eligible_market_count(self) -> int:
        return self.allocation_plan.eligible_market_count


@dataclass(frozen=True, slots=True)
class QuantDecision:
    """量化决策器输出——入场后所有 WS / 周期触发的决策统一返回此结构。

    actions:
        本次 trigger 下产生的 0/1/N 个 intent（SELL / replace / cancel / cover）。
    reason:
        决策理由，写入 audit。空字符串表示无理由（actions 也通常为空）。
    pause_trading / pause_reason:
        仅 reconcile_cycle 触发时使用——量化决策器发现系统性异常（如直播源全断、
        市场状态不一致）时，主动让 supervisor 暂停新入场。
    """

    actions: tuple[ExtensionDecision, ...] = ()
    reason: str = ""
    pause_trading: bool = False
    pause_reason: str = ""

    @property
    def has_actions(self) -> bool:
        return bool(self.actions or self.pause_trading)
