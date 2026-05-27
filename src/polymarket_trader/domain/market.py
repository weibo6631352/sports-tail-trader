from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class TradingStatus(StrEnum):
    CANDIDATE = "candidate"
    ELIGIBLE = "eligible"
    PAUSED = "paused"
    CLOSED = "closed"
    RESOLVED = "resolved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class MarketOutcome:
    token_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class Market:
    condition_id: str
    market_slug: str
    outcomes: tuple[MarketOutcome, ...]
    market_name: str | None = None
    market_question: str | None = None
    event_id: str | None = None
    event_title: str | None = None
    event_slug: str | None = None
    icon_url: str | None = None
    end_date: datetime | None = None
    game_start_time: datetime | None = None
    tick_size: Decimal = Decimal("0.01")
    # 来自 Polymarket 的 orderMinSize / min_order_size；这是市场订单 size 下限，
    # 不是本系统 .env 里的单笔 USDC 配置。BUY 时需结合价格理解其名义金额影响。
    min_order_size: Decimal = Decimal("1")
    neg_risk: bool = False
    fees_enabled: bool | None = None
    maker_base_fee_bps: int | None = None
    taker_base_fee_bps: int | None = None
    fee_rate_bps: int | None = None
    fee_rate_updated_at: datetime | None = None
    category: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    # Polymarket Gamma 的 sportsMarketType：对运动专属 prop 家族（method-of-victory、
    # F1 props、cricket props 等）可靠填充，对常规盘口多为空。workflow 层用它做精确的
    # prop 家族识别，避免泛化兜底拒绝原因（CLAUDE.md §17）。
    sports_market_type: str | None = None
    # 规范运动码（baseball / basketball / esports / football / ... 与
    # `INPLAY_COVERED_SPORTS` 对齐）。由 `sports.slug_resolver.resolve_sport_from_market`
    # 在 gamma payload → Market 转换时回填，下游 SportResolver / 健康面板 / 直播源
    # 订阅决策都读此字段，单一来源避免 R5 诊断里"mlbb 在 runtime 列表却不在
    # live_state token 表"那种漂移。None 表示推不出运动（多见于 outright / 非体育
    # 市场），调用方据此跳过 live source 订阅。
    sport: str | None = None
    matched_keywords: tuple[str, ...] = field(default_factory=tuple)
    trading_status: TradingStatus = TradingStatus.CANDIDATE
    reject_reason: str | None = None

    @property
    def token_ids(self) -> tuple[str, ...]:
        return tuple(outcome.token_id for outcome in self.outcomes)

    def get_outcome_by_token_id(self, token_id: str) -> MarketOutcome | None:
        for outcome in self.outcomes:
            if outcome.token_id == token_id:
                return outcome
        return None

    def get_outcome(self, outcome_name: str) -> MarketOutcome | None:
        normalized = outcome_name.strip().upper()
        for outcome in self.outcomes:
            if outcome.outcome.strip().upper() == normalized:
                return outcome
        return None

    def find_token_id(self, outcome_name: str) -> str | None:
        outcome = self.get_outcome(outcome_name)
        return None if outcome is None else outcome.token_id

    def require_token_id(self, outcome_name: str) -> str:
        token_id = self.find_token_id(outcome_name)
        if token_id is None:
            raise ValueError(f"market is missing outcome {outcome_name}")
        return token_id

    def with_trading_status(
        self,
        trading_status: TradingStatus,
        *,
        reject_reason: str | None = None,
    ) -> "Market":
        return replace(self, trading_status=trading_status, reject_reason=reject_reason)

    def with_tick_size(self, tick_size: Decimal) -> "Market":
        return replace(self, tick_size=tick_size)

    def with_min_order_size(self, min_order_size: Decimal) -> "Market":
        return replace(self, min_order_size=min_order_size)

    def with_fee_schedule(
        self,
        *,
        fees_enabled: bool | None = None,
        maker_base_fee_bps: int | None = None,
        taker_base_fee_bps: int | None = None,
    ) -> "Market":
        resolved_taker_base_fee_bps = (
            self.taker_base_fee_bps
            if taker_base_fee_bps is None
            else taker_base_fee_bps
        )
        return replace(
            self,
            fees_enabled=self.fees_enabled if fees_enabled is None else fees_enabled,
            maker_base_fee_bps=(
                self.maker_base_fee_bps
                if maker_base_fee_bps is None
                else maker_base_fee_bps
            ),
            taker_base_fee_bps=resolved_taker_base_fee_bps,
            fee_rate_bps=(
                self.fee_rate_bps
                if taker_base_fee_bps is None
                else resolved_taker_base_fee_bps
            ),
            fee_rate_updated_at=(
                self.fee_rate_updated_at
                if taker_base_fee_bps is None
                else None
            ),
        )

    def with_fee_rate(
        self,
        fee_rate_bps: int | None,
        *,
        fee_rate_updated_at: datetime | None = None,
    ) -> "Market":
        return replace(
            self,
            fee_rate_bps=fee_rate_bps,
            fee_rate_updated_at=(
                self.fee_rate_updated_at
                if fee_rate_updated_at is None
                else fee_rate_updated_at
            ),
        )

    def with_metadata(
        self,
        *,
        market_name: str | None = None,
        market_question: str | None = None,
        event_id: str | None = None,
        event_title: str | None = None,
        event_slug: str | None = None,
        icon_url: str | None = None,
        end_date: datetime | None = None,
        game_start_time: datetime | None = None,
        category: str | None = None,
        tags: tuple[str, ...] | None = None,
        matched_keywords: tuple[str, ...] | None = None,
        outcomes: tuple[MarketOutcome, ...] | None = None,
        neg_risk: bool | None = None,
    ) -> "Market":
        return replace(
            self,
            market_name=self.market_name if market_name is None else market_name,
            market_question=self.market_question if market_question is None else market_question,
            event_id=self.event_id if event_id is None else event_id,
            event_title=self.event_title if event_title is None else event_title,
            event_slug=self.event_slug if event_slug is None else event_slug,
            icon_url=self.icon_url if icon_url is None else icon_url,
            end_date=self.end_date if end_date is None else end_date,
            game_start_time=(
                self.game_start_time if game_start_time is None else game_start_time
            ),
            category=self.category if category is None else category,
            tags=self.tags if tags is None else tags,
            matched_keywords=(
                self.matched_keywords if matched_keywords is None else matched_keywords
            ),
            outcomes=self.outcomes if outcomes is None else outcomes,
            neg_risk=self.neg_risk if neg_risk is None else neg_risk,
        )


def market_status_allowed_for_manual_order(market: Market) -> bool:
    """市场状态是否允许人工下单（CLOSED/RESOLVED/REJECTED 都禁止）。

    用于 operator 受控操作（manual cancel/replace/force-exit）路径——CLOSED
    意味着 trading 已停，RESOLVED 是已结算，REJECTED 是被风控/discovery 拒。
    """
    return market.trading_status not in {
        TradingStatus.CLOSED,
        TradingStatus.RESOLVED,
        TradingStatus.REJECTED,
    }


def normalize_condition_ids(condition_ids: "Sequence[str] | None") -> tuple[str, ...]:
    """去除 None/空字符串后返回 tuple。"""
    if not condition_ids:
        return ()
    return tuple(condition_id for condition_id in condition_ids if condition_id)
