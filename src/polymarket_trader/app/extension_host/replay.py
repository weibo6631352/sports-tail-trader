from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from polymarket_trader.app.ports import build_extension_ports
from polymarket_trader.app.extension_host.loader import load_extension
from polymarket_trader.app.trading_decision_service import EntryPlan, TradingDecisionService
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import Order, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.extension_api import load_mapping_file


def run_entry_replay(
    fixture_path: str,
    *,
    extension_module: str | None = None,
    extension_config_path: str | None = None,
) -> dict[str, Any]:
    if extension_module is None:
        raise ValueError("extension_module is required for entry replay")
    fixture = load_mapping_file(fixture_path)
    registry = MarketRegistry()
    markets = tuple(_load_market(item) for item in _list(fixture, "markets"))
    for market in markets:
        registry.upsert(market)
    positions = tuple(_load_position(item) for item in _list(fixture, "positions"))
    open_orders = tuple(_load_order(item) for item in _list(fixture, "open_orders"))
    orderbooks = {
        orderbook.token_id: orderbook
        for orderbook in (_load_orderbook(item) for item in _list(fixture, "orderbooks"))
    }
    budgets = _mapping(fixture, "budgets")
    target = _mapping(fixture, "target")
    available_usdc = _optional_decimal(budgets, "available_usdc") or _decimal(
        budgets,
        "portfolio_budget_usdc",
    )
    account_state_store = AccountStateStore()
    account_state_store.update_balances(
        balance_usdc=available_usdc,
        allowance_usdc=available_usdc,
    )
    account_state_store.replace_positions(positions)
    account_state_store.replace_open_orders(open_orders)
    account_state_store.mark_user_ws_connected(True)
    account_state_store.mark_reconciled()
    extension = load_extension(
        module_path=extension_module,
        ports=build_extension_ports(
            registry=registry,
            snapshot_provider=account_state_store.snapshot,
            orderbook_reader=orderbooks.get,
        ),
        config_path=extension_config_path,
    )
    plan = TradingDecisionService(
        extension_hooks=extension.hooks,
        registry=registry,
        orderbook_reader=orderbooks.get,
    ).build_entry_plan(
        condition_id=_text(target, "condition_id"),
        token_id=_text(target, "token_id"),
        trace_id=_text(fixture, "trace_id") or "extension-replay",
        portfolio_budget_usdc=_decimal(budgets, "portfolio_budget_usdc"),
        available_usdc=available_usdc,
        max_order_usdc=_decimal(budgets, "max_order_usdc"),
        max_market_usdc=_decimal(budgets, "max_market_usdc"),
        max_total_usdc=_decimal(budgets, "max_total_usdc"),
        account_snapshot=account_state_store.snapshot(),
        metadata=_mapping(fixture, "metadata"),
    )
    return {
        "fixture_path": str(Path(fixture_path)),
        "extension": {
            "name": extension.spec.name,
            "extension_module": extension_module,
            "extension_config_path": extension_config_path,
            "capabilities": list(extension.spec.capabilities),
        },
        "plan": _serialize_plan(plan),
    }


def _load_market(item: Mapping[str, Any]) -> Market:
    return Market(
        condition_id=_text(item, "condition_id"),
        market_slug=_text(item, "market_slug"),
        outcomes=tuple(
            MarketOutcome(
                token_id=_text(outcome, "token_id"),
                outcome=_text(outcome, "outcome"),
            )
            for outcome in _list(item, "outcomes")
        ),
        market_name=_optional_text(item, "market_name"),
        market_question=_optional_text(item, "market_question"),
        event_id=_optional_text(item, "event_id"),
        event_title=_optional_text(item, "event_title"),
        event_slug=_optional_text(item, "event_slug"),
        end_date=_optional_datetime(item, "end_date"),
        game_start_time=_optional_datetime(item, "game_start_time"),
        category=_optional_text(item, "category"),
        tags=_text_tuple(item, "tags"),
        matched_keywords=tuple(str(keyword) for keyword in item.get("matched_keywords", ())),
        trading_status=TradingStatus(str(item.get("trading_status", TradingStatus.CANDIDATE.value))),
        tick_size=_optional_decimal(item, "tick_size") or Decimal("0.01"),
        min_order_size=_optional_decimal(item, "min_order_size") or Decimal("1"),
    )


def _load_orderbook(item: Mapping[str, Any]) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=_text(item, "token_id"),
        best_bid=_optional_decimal(item, "best_bid"),
        best_ask=_optional_decimal(item, "best_ask"),
        bids=tuple(_load_level(level) for level in _list(item, "bids")),
        asks=tuple(_load_level(level) for level in _list(item, "asks")),
        received_at=_datetime(item, "received_at"),
        market_slug=_optional_text(item, "market_slug"),
        condition_id=_optional_text(item, "condition_id"),
        best_bid_size=_optional_decimal(item, "best_bid_size"),
        best_ask_size=_optional_decimal(item, "best_ask_size"),
        tick_size=_optional_decimal(item, "tick_size"),
    )


def _load_level(item: Mapping[str, Any]) -> PriceLevel:
    return PriceLevel(price=_decimal(item, "price"), size=_decimal(item, "size"))


def _load_position(item: Mapping[str, Any]) -> Position:
    return Position(
        condition_id=_text(item, "condition_id"),
        token_id=_text(item, "token_id"),
        shares=_decimal(item, "shares"),
        cost_usdc=_decimal(item, "cost_usdc"),
        market_slug=_optional_text(item, "market_slug"),
        open_buy_shares=_optional_decimal(item, "open_buy_shares") or Decimal("0"),
        open_sell_shares=_optional_decimal(item, "open_sell_shares") or Decimal("0"),
    )


def _load_order(item: Mapping[str, Any]) -> Order:
    return Order(
        condition_id=_text(item, "condition_id"),
        token_id=_text(item, "token_id"),
        side=OrderSide(_text(item, "side")),
        order_type=OrderType(str(item.get("order_type", OrderType.GTC.value))),
        price=_decimal(item, "price"),
        trace_id=_optional_text(item, "trace_id") or "",
        market_slug=_optional_text(item, "market_slug"),
        amount_usdc=_optional_decimal(item, "amount_usdc"),
        size_shares=_optional_decimal(item, "size_shares"),
        remaining_shares=_optional_decimal(item, "remaining_shares"),
        order_id=_optional_text(item, "order_id"),
        idempotency_key=_optional_text(item, "idempotency_key"),
        status=OrderStatus(str(item.get("status", OrderStatus.LIVE.value))),
        reason=_optional_text(item, "reason") or "",
    )


def _serialize_plan(plan: EntryPlan) -> dict[str, Any]:
    return {
        "trace_id": plan.trace_id,
        "reason": plan.reason,
        "ready_to_trade": plan.ready_to_trade,
        "eligible_market_count": plan.eligible_market_count,
        "metadata": _jsonish(plan.metadata or {}),
        "market": None
        if plan.market is None
        else {
            "condition_id": plan.market.condition_id,
            "market_slug": plan.market.market_slug,
            "token_id": (
                None
                if plan.intent is None
                else plan.intent.token_id
            ) or (
                None
                if plan.allocation is None
                else plan.allocation.token_id
            ),
        },
        "allocation_plan": {
            "trace_id": plan.allocation_plan.trace_id,
            "total_budget_usdc": str(plan.allocation_plan.total_budget_usdc),
            "allocated_budget_usdc": str(plan.allocation_plan.allocated_budget_usdc),
            "released_budget_usdc": str(plan.allocation_plan.released_budget_usdc),
            "reason": plan.allocation_plan.reason,
        },
        "allocation": None
        if plan.allocation is None
        else {
            "condition_id": plan.allocation.condition_id,
            "token_id": plan.allocation.token_id,
            "target_budget_usdc": str(plan.allocation.target_budget_usdc),
            "buy_budget_usdc": str(plan.allocation.buy_budget_usdc),
            "reason": plan.allocation.reason,
            "release_reason": plan.allocation.release_reason,
        },
        "intent": None
        if plan.intent is None
        else {
            "condition_id": plan.intent.condition_id,
            "token_id": plan.intent.token_id,
            "price": str(plan.intent.price),
            "amount_usdc": None if getattr(plan.intent, "amount_usdc", None) is None else str(plan.intent.amount_usdc),
            "size_shares": None if getattr(plan.intent, "size_shares", None) is None else str(plan.intent.size_shares),
            "market_slug": plan.intent.market_slug,
            "order_type": plan.intent.order_type.value,
        },
    }


def _mapping(item: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = item.get(key, {})
    return value if isinstance(value, Mapping) else {}


def _jsonish(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonish(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonish(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _list(item: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = item.get(key, ())
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]


def _text_tuple(item: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = item.get(key, ())
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(entry) for entry in value if entry is not None)


def _text(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if value is None:
        return ""
    return str(value)


def _optional_text(item: Mapping[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    return str(value)


def _decimal(item: Mapping[str, Any], key: str) -> Decimal:
    return Decimal(str(item.get(key)))


def _optional_decimal(item: Mapping[str, Any], key: str) -> Decimal | None:
    value = item.get(key)
    if value is None:
        return None
    return Decimal(str(value))


def _datetime(item: Mapping[str, Any], key: str) -> datetime:
    value = _text(item, key)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _optional_datetime(item: Mapping[str, Any], key: str) -> datetime | None:
    value = item.get(key)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)
