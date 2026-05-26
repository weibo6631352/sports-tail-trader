"""量化决策相关的所有数据类型。

汇总自原 contracts/ 目录的 decisions / summary / manual_confirmation / context
四份文件。整个交易系统只有一个量化决策器，不存在"框架抽象 + workflow 实现"分层，
这些数据类型直接放在 domain/ 里作为业务 domain 一等公民。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Protocol

from polymarket_trader.domain.account import MarketPause
from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import Order, OrderResult, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.sports_live import GoalserveOddsSample, SoccerMatchEvent


class TradeAction(StrEnum):
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
    """决策的语义分类。framework 用 decision_kind 而不是 metadata 字符串
    判断"该决策是入场 / 退场 / 跟单 / 恢复"。决策时必须显式声明。"""

    ENTRY = "entry"
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
class TradingDecision:
    action: TradeAction
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
    summary: DecisionSummary | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def skip(
        cls,
        *,
        reason: str,
        decision_kind: DecisionKind | None = None,
        intent_tags: frozenset[str] | None = None,
        summary: DecisionSummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TradingDecision":
        return cls(
            action=TradeAction.SKIP,
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
        summary: DecisionSummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TradingDecision":
        return cls(
            action=TradeAction.BUY,
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
        summary: DecisionSummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TradingDecision":
        return cls(
            action=TradeAction.SELL,
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
        summary: DecisionSummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TradingDecision":
        return cls(
            action=TradeAction.CANCEL,
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
        summary: DecisionSummary | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TradingDecision":
        return cls(
            action=TradeAction.REPLACE,
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

    actions: tuple[TradingDecision, ...] = ()
    reason: str = ""
    pause_trading: bool = False
    pause_reason: str = ""

    @property
    def has_actions(self) -> bool:
        return bool(self.actions or self.pause_trading)


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    """量化决策器主动提供给 framework 的中性展示视图。

    framework 的 operator / UI / audit / 复盘读这里的字段，不读决策私有 metadata。
    调用方可把任何 framework 不解析、但 operator 详情页希望透传给前端的扩展结构放进
    ``extras``。framework 对 ``extras`` 整体序列化、不按字段名解释。
    """

    action: str = ""
    reason: str = ""
    label: str = ""
    market_type: str = ""
    side: str = ""
    line: Decimal | None = None
    best_ask: Decimal | None = None
    observed_at: datetime | None = None
    manual_confirmed: bool = False
    confirmed_by: str = ""
    confirm_reason: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)


# ----- ManualConfirmation -----
# operator 触发候选确认时，framework 把这个 DTO 注入 ``DecisionContext.manual_confirmation``。

@dataclass(frozen=True, slots=True)
class ManualConfirmation:
    """单次人工确认的可审计凭证。"""

    operator: str
    reason: str = "manual_confirm"
    confirmed_at: datetime | None = None


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

    @property
    def fills(self) -> tuple[Fill, ...]: ...

    def get_position(self, condition_id: str, token_id: str) -> Position | None: ...

    def is_market_paused(self, condition_id: str) -> bool: ...

    def open_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]: ...

    def open_buy_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]: ...

    def open_sell_orders_for_market(self, condition_id: str, token_id: str) -> tuple[Order, ...]: ...


@dataclass(frozen=True, slots=True)
class SignalHistory:
    """决策时点的时序信号源快照——按 condition_id 聚合的时间序列。

    DecisionContext 持有的是 buffer 在决策时点的**只读快照**，不是 buffer 实例
    本身——domain 层不依赖 runtime/。每个字段是 frozen tuple，调用方拿到后可
    跨线程/异步任务消费，buffer 自身在主 loop 继续追加不影响此处。

    扩展时按"一个信号源一个字段"加：``orderbook_history`` /
    ``live_state_history`` 等后续接入。
    """

    match_events: tuple[SoccerMatchEvent, ...] = ()
    goalserve_odds: tuple[GoalserveOddsSample, ...] = ()


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """框架向workflow hook 输入的上下文。所有字段直接读取，不通过 sub-view 属性中转。"""

    trace_id: str
    # 框架内部禁止填默认值；workflow manifest/spec 提供单一来源（CLAUDE.md §10）。
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
    allocation_plan: AllocationPlan | None = None
    allocation: Allocation | None = None
    amount_usdc: Decimal | None = None
    size_shares: Decimal | None = None
    manual_confirmation: ManualConfirmation | None = None
    # quant_decide 触发源——仅 Workflow 2 (WS / 周期 触发) 使用；entry path 不填。
    quant_trigger_kind: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    signal_history: SignalHistory = field(default_factory=SignalHistory)



# ===== DecisionRecord — workflow hook 调用录制（DB 持久化用） =====
# 与上方 TradingDecision 系列类型同属 domain 决策层，但 DecisionRecord 是
# "决策审计行"——录制 quant_decide 等 hook 一次调用的输入快照 + 输出 decision，
# 由 ``infra/db/models/decision.py`` 持久化到 ``decision_records`` 表。
from uuid import uuid4


def _decision_record_utc_now() -> datetime:
    from datetime import timezone as _tz
    return datetime.now(_tz.utc)


def _decision_record_normalize_datetime(value: datetime | None) -> datetime:
    from datetime import timezone as _tz
    value = value or _decision_record_utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=_tz.utc)
    return value.astimezone(_tz.utc)


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """单次 hook 调用录制条目（持久化到 decision_records 表）。"""

    trace_id: str
    condition_id: str
    decision_input: Mapping[str, Any]
    decision_output: Mapping[str, Any]
    accepted: bool
    hook_name: str = ""
    token_id: str | None = None
    market_slug: str | None = None
    reason: str | None = None
    record_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=_decision_record_utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _decision_record_normalize_datetime(self.created_at))
