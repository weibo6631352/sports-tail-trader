"""AdminService 内部使用的纯函数 helper、类型别名与值对象。

这里只放无状态的辅助逻辑（市场过滤/排序、候选匹配、直播源缺口投影）以及供
`_with_repositories` 返回的 `_RepositoryGroup` 等值类型；所有有状态的编排
仍留在 ``admin_service.py`` 的 ``AdminService`` 类内。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.infra.db import (
    AllocationRepository,
    AuditEventRepository,
    DecisionRecordRepository,
    FillRepository,
    MarketRepository,
    OrderRepository,
    OutboxEventRepository,
    PositionRepository,
)


MarketFeeSortField = Literal[
    "market_slug",
    "fee_rate_bps",
    "fee_rate_updated_at",
    "maker_base_fee_bps",
    "taker_base_fee_bps",
]
SortDirection = Literal["asc", "desc"]

_LIVE_SOURCE_GAP_PAST_WINDOW = timedelta(days=2)


@dataclass(frozen=True, slots=True)
class _RepositoryGroup:
    audit: AuditEventRepository
    market: MarketRepository
    order: OrderRepository
    fill: FillRepository
    position: PositionRepository
    allocation: AllocationRepository
    decision: DecisionRecordRepository
    outbox: OutboxEventRepository


def _orderbook_has_no_quotes(snapshot: OrderbookSnapshot) -> bool:
    """判断热态盘口是否只是空占位。

    Market WS worker 会先为已跟踪 token 建立空快照；查询侧不能把这种快照当作
    有效盘口，否则会遮蔽 REST 中已经存在的真实买卖盘。
    """

    return (
        snapshot.best_bid is None
        and snapshot.best_ask is None
        and not snapshot.bids
        and not snapshot.asks
    )


def _market_matches_fee_filters(
    market: Market,
    *,
    fees_enabled: bool | None = None,
    fee_rate_bps_min: int | None = None,
    fee_rate_bps_max: int | None = None,
    maker_base_fee_bps_min: int | None = None,
    maker_base_fee_bps_max: int | None = None,
    taker_base_fee_bps_min: int | None = None,
    taker_base_fee_bps_max: int | None = None,
) -> bool:
    if fees_enabled is not None and market.fees_enabled is not fees_enabled:
        return False
    if fee_rate_bps_min is not None and (market.fee_rate_bps is None or market.fee_rate_bps < fee_rate_bps_min):
        return False
    if fee_rate_bps_max is not None and (market.fee_rate_bps is None or market.fee_rate_bps > fee_rate_bps_max):
        return False
    if maker_base_fee_bps_min is not None and (
        market.maker_base_fee_bps is None or market.maker_base_fee_bps < maker_base_fee_bps_min
    ):
        return False
    if maker_base_fee_bps_max is not None and (
        market.maker_base_fee_bps is None or market.maker_base_fee_bps > maker_base_fee_bps_max
    ):
        return False
    if taker_base_fee_bps_min is not None and (
        market.taker_base_fee_bps is None or market.taker_base_fee_bps < taker_base_fee_bps_min
    ):
        return False
    if taker_base_fee_bps_max is not None and (
        market.taker_base_fee_bps is None or market.taker_base_fee_bps > taker_base_fee_bps_max
    ):
        return False
    return True


def _market_sort_value(market: Market, sort_by: MarketFeeSortField) -> object | None:
    return {
        "market_slug": market.market_slug,
        "fee_rate_bps": market.fee_rate_bps,
        "fee_rate_updated_at": market.fee_rate_updated_at,
        "maker_base_fee_bps": market.maker_base_fee_bps,
        "taker_base_fee_bps": market.taker_base_fee_bps,
    }[sort_by]


def _sort_markets(
    markets: Sequence[Market],
    *,
    sort_by: MarketFeeSortField | None = None,
    sort_direction: SortDirection = "desc",
) -> tuple[Market, ...]:
    if sort_by is None:
        return tuple(markets)
    present = [market for market in markets if _market_sort_value(market, sort_by) is not None]
    missing = [market for market in markets if _market_sort_value(market, sort_by) is None]
    present.sort(
        key=lambda market: cast(Any, _market_sort_value(market, sort_by)),
        reverse=sort_direction == "desc",
    )
    return tuple(present + missing)


def _candidate_matches_filters(
    candidate: Mapping[str, Any],
    *,
    market_type: str | None,
    game_status: str | None,
    action: str | None,
    execution_permission: str | None,
    accepted: bool | None,
    confirmable: bool | None,
    league: str | None,
) -> bool:
    """判断候选投影是否满足管理台筛选条件。"""

    if not _text_filter_matches(candidate.get("market_type"), market_type):
        return False
    if not _text_filter_matches(candidate.get("game_status"), game_status):
        return False
    if not _text_filter_matches(candidate.get("action"), action):
        return False
    if not _text_filter_matches(candidate.get("execution_permission"), execution_permission):
        return False
    if not _text_filter_matches(candidate.get("league"), league):
        return False
    if accepted is not None and bool(candidate.get("accepted")) is not accepted:
        return False
    if confirmable is not None and bool(candidate.get("confirmable")) is not confirmable:
        return False
    return True


def _text_filter_matches(value: object, expected: str | None) -> bool:
    if expected is None or not expected.strip():
        return True
    return str(value or "").strip().lower() == expected.strip().lower()


def _market_slug_prefix(market: Market) -> str:
    """提取 market slug 的首段，用于直播源缺口聚合。"""

    slug = (market.market_slug or market.event_slug or "").strip().lower()
    if not slug:
        return "unknown"
    return slug.split("-", 1)[0] or "unknown"


def _live_source_gap_scope_markets(runtime: Any, markets: Sequence[Market]) -> tuple[Market, ...]:
    """返回适用于单场直播源覆盖诊断的市场集合。

    直播比分源只适合直接匹配单场市场。系列赛、冠军、奖项、转会/下家等长期
    市场也属于体育策略目标，但需要专用数据源（赛季状态、隐含概率）和定价
    模型；它们不应被计入 live-source gap，避免把诊断噪声误当成单场直播源缺口。
    universe 现在接受 single_game ∪ outright，因此这里再用 metadata 过滤出
    family == single_game 的子集。
    """

    hooks = _runtime_extension_hooks(runtime)
    if hooks is None:
        return tuple(markets)
    scoped: list[Market] = []
    for market in markets:
        try:
            decision = hooks.select_market(market)
        except Exception:
            continue
        if not decision.selected:
            continue
        if decision.metadata.get("market_family") not in (None, "single_game"):
            # outright / series / esports 不依赖单场直播源，本诊断不覆盖。
            continue
        scoped.append(market)
    return tuple(scoped)


def _runtime_extension_hooks(runtime: Any) -> Any | None:
    """提取运行时已装配的扩展 hooks。

    Admin 查询不直接依赖具体策略包；优先使用 runtime.extension，并在 API 运行时
    只暴露应用服务对象时，从 MarketService 读取同一份 universe hooks。
    """

    extension = getattr(runtime, "extension", None)
    hooks = getattr(extension, "hooks", None)
    if hooks is not None:
        return hooks
    market_service = getattr(runtime, "market_service", None)
    hooks = getattr(market_service, "extension_hooks", None)
    if hooks is not None:
        return hooks
    return getattr(market_service, "_extension_hooks", None)


def _live_source_gap_urgency(market: Market, *, now: datetime) -> str:
    """按开赛时间给直播源缺口分配实盘排查优先级。"""

    start_time = _ensure_utc(market.game_start_time)
    if start_time is None:
        return "unknown_time"
    if start_time <= now:
        return "started_or_past_due"
    if start_time <= now + timedelta(hours=24):
        return "starts_within_24h"
    return "future_schedule"


def _live_source_gap_outside_diagnostic_window(market: Market, *, now: datetime) -> bool:
    """过滤直播源已不再稳定保留的陈旧开赛市场，避免缺口统计被历史噪声淹没。"""

    start_time = _ensure_utc(market.game_start_time)
    if start_time is None:
        return False
    return start_time < now - _LIVE_SOURCE_GAP_PAST_WINDOW


def _live_source_gap_urgency_rank(urgency: str) -> int:
    """返回直播源缺口优先级排序权重。"""

    ranks = {
        "started_or_past_due": 0,
        "starts_within_24h": 1,
        "future_schedule": 2,
        "unknown_time": 3,
    }
    return ranks.get(urgency, 99)


def _ensure_utc(value: datetime | None) -> datetime | None:
    """把可选时间规范成 UTC aware datetime。"""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _live_source_gap_market_payload(market: Market, *, now: datetime) -> dict[str, Any]:
    """把缺少直播状态的 market 转成诊断样本。"""

    start_time = _ensure_utc(market.game_start_time)
    end_date = _ensure_utc(market.end_date)
    return {
        "condition_id": market.condition_id,
        "market_slug": market.market_slug,
        "event_slug": market.event_slug,
        "slug_prefix": _market_slug_prefix(market),
        "gap_urgency": _live_source_gap_urgency(market, now=now),
        "game_start_time": None if start_time is None else start_time.isoformat(),
        "end_date": None if end_date is None else end_date.isoformat(),
        "market_question": market.market_question,
        "event_title": market.event_title,
        "category": market.category,
        "tags": tuple(market.tags),
        "trading_status": market.trading_status.value,
        "outcome_count": len(market.outcomes),
    }
