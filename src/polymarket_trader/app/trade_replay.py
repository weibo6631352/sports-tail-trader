"""成交、盈亏和结算只读复盘聚合。

本模块从已存在的 market、orders、fills、positions 和 audit events 构建统一复盘视图。
它不作为交易账本的唯一事实来源，也不改变主交易链路；真实成交以 fills 和外部
Data API position PnL 字段为准，audit payload 只用于串联候选原因和退出计划。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from polymarket_trader.app.admin_serialization import AdminSerializer, decimal_text
from polymarket_trader.domain.events import AuditEvent, Fill
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.position import Position
from polymarket_trader.serialization import jsonable


@dataclass(frozen=True, slots=True)
class TradeReplayFilters:
    """成交复盘查询过滤条件。"""

    condition_id: str | None = None
    token_id: str | None = None
    trace_id: str | None = None


def build_trade_replay_records(
    *,
    markets: Sequence[Market],
    orders: Sequence[Order],
    fills: Sequence[Fill],
    positions: Sequence[Position],
    audit_events: Sequence[AuditEvent],
    serializer: AdminSerializer,
    filters: TradeReplayFilters | None = None,
) -> tuple[dict[str, Any], ...]:
    """按 ``condition_id/token_id`` 聚合真实成交后的复盘记录。"""

    filters = filters or TradeReplayFilters()
    markets_by_condition = {market.condition_id: market for market in markets}
    filtered_orders = tuple(order for order in orders if _matches_order(order, filters))
    filtered_fills = tuple(fill for fill in fills if _matches_fill(fill, filters))
    filtered_audits = tuple(event for event in audit_events if _matches_audit(event, filters))
    keys = _replay_keys(
        orders=filtered_orders,
        fills=filtered_fills,
        positions=positions,
        audit_events=filtered_audits,
        filters=filters,
    )
    records = [
        _build_record(
            key=key,
            markets_by_condition=markets_by_condition,
            orders=filtered_orders,
            fills=filtered_fills,
            positions=positions,
            audit_events=filtered_audits,
            serializer=serializer,
        )
        for key in keys
    ]
    return tuple(sorted(records, key=_record_sort_key, reverse=True))


def _build_record(
    *,
    key: tuple[str, str],
    markets_by_condition: Mapping[str, Market],
    orders: Sequence[Order],
    fills: Sequence[Fill],
    positions: Sequence[Position],
    audit_events: Sequence[AuditEvent],
    serializer: AdminSerializer,
) -> dict[str, Any]:
    condition_id, token_id = key
    market = markets_by_condition.get(condition_id)
    outcome = None if market is None else market.get_outcome_by_token_id(token_id)
    item_orders = tuple(order for order in orders if order.condition_id == condition_id and order.token_id == token_id)
    item_fills = tuple(fill for fill in fills if fill.condition_id == condition_id and fill.token_id == token_id)
    item_audits = tuple(
        event for event in audit_events if event.condition_id == condition_id and event.token_id == token_id
    )
    position = next(
        (
            item
            for item in positions
            if item.condition_id == condition_id and item.token_id == token_id
        ),
        None,
    )
    buy = _fill_leg(item_fills, side="buy")
    sell = _fill_leg(item_fills, side="sell")
    pnl = _pnl_payload(buy=buy, sell=sell, position=position)
    audit_payload = _audit_context(item_audits)
    return {
        "condition_id": condition_id,
        "token_id": token_id,
        "market_slug": None if market is None else market.market_slug,
        "event_slug": None if market is None else market.event_slug,
        "event_title": None if market is None else market.event_title,
        "outcome": None if outcome is None else outcome.outcome,
        "market_status": None if market is None else market.trading_status.value,
        "settlement_status": _settlement_status(market=market, position=position),
        "trace_ids": sorted({item.trace_id for item in (*item_orders, *item_fills, *item_audits) if item.trace_id}),
        "order_ids": sorted({item.order_id for item in (*item_orders, *item_fills, *item_audits) if item.order_id}),
        "trade_ids": sorted({item.trade_id for item in (*item_orders, *item_fills, *item_audits) if item.trade_id}),
        "buy": buy,
        "sell": sell,
        "position": None if position is None else serializer.position(position),
        "pnl": pnl,
        "strategy_summary": audit_payload.get("strategy_summary"),
        "decision_kind": audit_payload.get("decision_kind"),
        "strategy_payload": audit_payload.get("strategy_payload"),
        "candidate_reasons": audit_payload.get("candidate_reasons"),
        "first_fill_at": jsonable(min((_fill_time(fill) for fill in item_fills), default=None)),
        "last_fill_at": jsonable(max((_fill_time(fill) for fill in item_fills), default=None)),
        "last_position_updated_at": None if position is None else jsonable(position.updated_at),
        "source_counts": {
            "orders": len(item_orders),
            "fills": len(item_fills),
            "audit_events": len(item_audits),
            "position": 0 if position is None else 1,
        },
    }


def _pnl_payload(
    *,
    buy: Mapping[str, Any],
    sell: Mapping[str, Any],
    position: Position | None,
) -> dict[str, Any]:
    buy_size = _decimal_value(buy.get("size"))
    buy_notional = _decimal_value(buy.get("notional_usdc"))
    sell_size = _decimal_value(sell.get("size"))
    sell_notional = _decimal_value(sell.get("notional_usdc"))
    avg_buy_price = buy_notional / buy_size if buy_size > 0 else None
    realized_from_fills = None
    if avg_buy_price is not None and sell_size > 0:
        realized_from_fills = sell_notional - avg_buy_price * sell_size
    authoritative_realized = None if position is None else position.realized_pnl
    authoritative_cash = None if position is None else position.cash_pnl
    source = "data_position" if authoritative_realized is not None or authoritative_cash is not None else "fills"
    return {
        "source": source,
        "realized_pnl_usdc": decimal_text(authoritative_realized if authoritative_realized is not None else realized_from_fills),
        "cash_pnl_usdc": decimal_text(authoritative_cash),
        "current_value_usdc": decimal_text(None if position is None else position.current_value),
        "initial_value_usdc": decimal_text(None if position is None else position.initial_value),
        "open_cost_usdc": decimal_text(None if position is None else position.cost_usdc),
        "net_cashflow_usdc": decimal_text(sell_notional - buy_notional),
        "avg_buy_price": decimal_text(avg_buy_price),
        "cur_price": decimal_text(None if position is None else position.cur_price),
        "redeemable": None if position is None else position.redeemable,
        "warning": "sell_size_exceeds_buy_size" if buy_size > 0 and sell_size > buy_size else None,
        "data_sources": (
            ["fills", "data_position"]
            if source == "data_position"
            else ["fills"]
        ),
    }


def _fill_leg(fills: Sequence[Fill], *, side: str) -> dict[str, Any]:
    matched = tuple(fill for fill in fills if _side_text(fill.side) == side)
    size = sum((_decimal_or_zero(fill.size) for fill in matched), Decimal("0"))
    notional = sum((_fill_notional(fill) for fill in matched), Decimal("0"))
    return {
        "count": len(matched),
        "size": decimal_text(size),
        "notional_usdc": decimal_text(notional),
        "avg_price": decimal_text(notional / size if size > 0 else None),
    }


def _audit_context(audit_events: Sequence[AuditEvent]) -> dict[str, Any]:
    """从 audit events 提取 framework 中性的复盘字段：strategy_summary 强类型、
    decision_kind 枚举值、strategy_payload 策略私有透传 dict。framework 不再按
    具体 metadata key 名（如 live_game）查找——一律读 strategy_payload 整体。
    """

    reasons: list[str] = []
    result: dict[str, Any] = {
        "candidate_reasons": reasons,
        "strategy_summary": None,
        "decision_kind": None,
        "strategy_payload": None,
    }
    for event in audit_events:
        if event.reason:
            reasons.append(event.reason)
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        plan_payload = payload.get("plan_metadata")
        if not isinstance(plan_payload, Mapping):
            plan_payload = payload
        if result["strategy_summary"] is None:
            summary = plan_payload.get("strategy_summary") if isinstance(plan_payload, Mapping) else None
            if isinstance(summary, Mapping):
                result["strategy_summary"] = dict(summary)
        if result["decision_kind"] is None:
            kind = plan_payload.get("decision_kind") if isinstance(plan_payload, Mapping) else None
            if kind:
                result["decision_kind"] = kind
        if result["strategy_payload"] is None:
            sp = plan_payload.get("strategy_payload") if isinstance(plan_payload, Mapping) else None
            if isinstance(sp, Mapping):
                result["strategy_payload"] = dict(sp)
    return result


def _replay_keys(
    *,
    orders: Sequence[Order],
    fills: Sequence[Fill],
    positions: Sequence[Position],
    audit_events: Sequence[AuditEvent],
    filters: TradeReplayFilters,
) -> tuple[tuple[str, str], ...]:
    keys: set[tuple[str, str]] = set()
    for item in (*orders, *fills, *audit_events):
        key = _key(item.condition_id, item.token_id)
        if key is not None:
            keys.add(key)
    if filters.trace_id is None:
        for position in positions:
            key = _key(position.condition_id, position.token_id)
            if key is not None and _matches_position(position, filters):
                keys.add(key)
    for position in positions:
        key = _key(position.condition_id, position.token_id)
        if key is not None and key in keys and _matches_position(position, filters):
            keys.add(key)
    return tuple(sorted(keys))


def _matches_order(order: Order, filters: TradeReplayFilters) -> bool:
    return (
        (filters.condition_id is None or order.condition_id == filters.condition_id)
        and (filters.token_id is None or order.token_id == filters.token_id)
        and (filters.trace_id is None or order.trace_id == filters.trace_id)
    )


def _matches_fill(fill: Fill, filters: TradeReplayFilters) -> bool:
    return (
        (filters.condition_id is None or fill.condition_id == filters.condition_id)
        and (filters.token_id is None or fill.token_id == filters.token_id)
        and (filters.trace_id is None or fill.trace_id == filters.trace_id)
    )


def _matches_audit(event: AuditEvent, filters: TradeReplayFilters) -> bool:
    return (
        (filters.condition_id is None or event.condition_id == filters.condition_id)
        and (filters.token_id is None or event.token_id == filters.token_id)
        and (filters.trace_id is None or event.trace_id == filters.trace_id)
    )


def _matches_position(position: Position, filters: TradeReplayFilters) -> bool:
    return (
        (filters.condition_id is None or position.condition_id == filters.condition_id)
        and (filters.token_id is None or position.token_id == filters.token_id)
    )


def _settlement_status(*, market: Market | None, position: Position | None) -> str:
    if position is not None and position.redeemable is True:
        return "redeemable"
    if market is not None and market.trading_status == TradingStatus.RESOLVED:
        return "resolved"
    if market is not None and market.trading_status == TradingStatus.CLOSED:
        return "closed"
    return "open"


def _record_sort_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("last_fill_at") or ""),
        str(record.get("last_position_updated_at") or ""),
        str(record.get("condition_id") or ""),
    )


def _key(condition_id: str | None, token_id: str | None) -> tuple[str, str] | None:
    if not condition_id or not token_id:
        return None
    return str(condition_id), str(token_id)


def _fill_time(fill: Fill) -> datetime:
    return fill.confirmed_at or fill.created_at


def _side_text(value: object) -> str:
    return str(value or "").strip().lower()


def _fill_notional(fill: Fill) -> Decimal:
    if fill.notional_usdc is not None:
        return fill.notional_usdc
    if fill.price is not None and fill.size is not None:
        return fill.price * fill.size
    return Decimal("0")


def _decimal_or_zero(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal("0")


def _decimal_value(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


