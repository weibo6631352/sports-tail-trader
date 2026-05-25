from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from polymarket_trader.app.reconcile_service import ReconcileAction, ReconcilePlan
from polymarket_trader.app.order_gateway import TradingReviewResult
from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.fees import FeeQuote, TakerFeePreview, build_taker_fee_preview
from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.order import Order, OrderResult
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.position_lifecycle import classify as classify_position_lifecycle
from polymarket_trader.infra.db import RepositoryPage
from polymarket_trader.infra.polymarket import ClobPriceHistoryDTO
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.serialization import decimal_text, jsonable


# token_id 是 256-bit 整数；JSONB payload 里历史上存成 int，对外契约必须是 string。
_TOKEN_ID_LIKE_KEYS = frozenset({"token_id", "winning_token_id"})


def _normalize_token_id_strings(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (
                str(v)
                if k in _TOKEN_ID_LIKE_KEYS and isinstance(v, int) and not isinstance(v, bool)
                else _normalize_token_id_strings(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_normalize_token_id_strings(item) for item in value]
    return value


def page_payload(page: RepositoryPage[Any], *, serializer: Callable[[Any], Any]) -> dict[str, Any]:
    return {
        "items": [serializer(item) for item in page.items],
        # total=-1 是 sentinel:调用方传 with_total=False 跳过了 count subquery
        "total": page.total if page.total >= 0 else None,
        "limit": page.limit,
        "offset": page.offset,
    }


@dataclass(frozen=True, slots=True)
class AdminSerializer:
    account_snapshot_provider: Callable[[], AccountSnapshot]
    registry_snapshot_provider: Callable[[], MarketRegistrySnapshot]
    market_ws_snapshot: Callable[[str], OrderbookSnapshot | None]

    def portfolio_snapshot(self, account: AccountSnapshot) -> dict[str, Any]:
        return {
            "balance_usdc": decimal_text(account.balance_usdc),
            "allowance_usdc": decimal_text(account.allowance_usdc),
            "open_buy_reserved_usdc": decimal_text(account.open_buy_reserved_usdc),
            "available_usdc": decimal_text(account.available_usdc),
            "positions": len(account.positions),
            "open_orders": len(account.open_orders),
            "fills": len(account.fills),
            "market_pauses": [pause.as_payload() for pause in account.market_pauses],
            "allow_new_entries": account.allow_new_entries,
            "user_ws_connected": account.user_ws_connected,
            "last_reconcile_at": jsonable(account.last_reconcile_at),
        }

    def account_snapshot(self, account: AccountSnapshot) -> dict[str, Any]:
        return {
            "balance_usdc": decimal_text(account.balance_usdc),
            "allowance_usdc": decimal_text(account.allowance_usdc),
            "open_buy_reserved_usdc": decimal_text(account.open_buy_reserved_usdc),
            "available_usdc": decimal_text(account.available_usdc),
            "positions": [jsonable(position) for position in account.positions],
            "open_orders": [jsonable(order) for order in account.open_orders],
            "fills": [jsonable(fill) for fill in account.fills],
            "user_ws_connected": account.user_ws_connected,
            "allow_new_entries": account.allow_new_entries,
            "market_pauses": [pause.as_payload() for pause in account.market_pauses],
            "last_reconcile_at": jsonable(account.last_reconcile_at),
        }

    def market_view(
        self,
        market: Market,
        *,
        account_snapshot: AccountSnapshot | None = None,
        registry_snapshot: MarketRegistrySnapshot | None = None,
    ) -> dict[str, Any]:
        if account_snapshot is None:
            account_snapshot = self.account_snapshot_provider()
        if registry_snapshot is None:
            registry_snapshot = self.registry_snapshot_provider()
        token_views = [
            self.token_view(
                market,
                outcome=outcome,
                orderbook=self.market_ws_snapshot(outcome.token_id),
                account_snapshot=account_snapshot,
            )
            for outcome in market.outcomes
        ]
        market_dict = self.market(market)
        fees = market_dict.get("fees") or {}
        return {
            **market_dict,
            "tracked": registry_snapshot.get_by_condition_id(market.condition_id) is not None,
            "token_views": token_views,
            # 列表/详情页直接读 fee_preview：把 market 级 fees 折叠成与 token_view.fee_preview 同构的 shape
            "fee_preview": {
                "fee_rate_bps": fees.get("fee_rate_bps"),
                "fees_enabled": fees.get("enabled"),
                "maker_base_fee_bps": fees.get("maker_base_fee_bps"),
                "taker_base_fee_bps": fees.get("taker_base_fee_bps"),
                "fee_rate_updated_at": fees.get("fee_rate_updated_at"),
            },
            # 前端 marketBadges 读 pause_reason；当前来源是 market.reject_reason
            "pause_reason": market_dict.get("reject_reason"),
            "tokens": [
                {
                    "token_id": tv.get("token_id"),
                    "outcome": tv.get("outcome"),
                    "position_size_shares": (tv.get("position") or {}).get("size_shares"),
                }
                for tv in token_views
            ],
        }

    def token_view(
        self,
        market: Market,
        *,
        outcome: MarketOutcome,
        orderbook: OrderbookSnapshot | None,
        account_snapshot: AccountSnapshot,
    ) -> dict[str, Any]:
        position = account_snapshot.get_position(market.condition_id, outcome.token_id)
        open_orders = account_snapshot.open_orders_for_market(market.condition_id, outcome.token_id)
        return {
            "token_id": outcome.token_id,
            "outcome": outcome.outcome,
            "orderbook": self.orderbook(orderbook),
            "position": self.position(position) if position is not None else None,
            "open_orders": [self.order(order) for order in open_orders],
            "open_order_count": len(open_orders),
            "best_ask": decimal_text(orderbook.best_ask) if orderbook is not None else None,
            "best_bid": decimal_text(orderbook.best_bid) if orderbook is not None else None,
            "spread": decimal_text(orderbook.spread) if orderbook is not None else None,
            "fee_preview": self.fee_preview(
                build_taker_fee_preview(
                    market=market,
                    orderbook=orderbook,
                )
            ),
        }

    def market(self, market: Market) -> dict[str, Any]:
        return {
            "condition_id": market.condition_id,
            "market_slug": market.market_slug,
            "event_slug": market.event_slug,
            "event_id": market.event_id,
            "event_title": market.event_title,
            "token_ids": list(market.token_ids),
            "outcomes": [
                {
                    "token_id": outcome.token_id,
                    "outcome": outcome.outcome,
                }
                for outcome in market.outcomes
            ],
            "icon_url": market.icon_url,
            "end_date": jsonable(market.end_date),
            "game_start_time": jsonable(market.game_start_time),
            "tick_size": decimal_text(market.tick_size),
            "min_order_size": decimal_text(market.min_order_size),
            "neg_risk": market.neg_risk,
            "fees": {
                "enabled": market.fees_enabled,
                "maker_base_fee_bps": market.maker_base_fee_bps,
                "taker_base_fee_bps": market.taker_base_fee_bps,
                "fee_rate_bps": market.fee_rate_bps,
                "fee_rate_updated_at": jsonable(market.fee_rate_updated_at),
            },
            "category": market.category,
            "tags": list(market.tags),
            "matched_keywords": list(market.matched_keywords),
            "trading_status": market.trading_status.value,
            "reject_reason": market.reject_reason,
        }

    def fee_preview(self, preview: TakerFeePreview | None) -> dict[str, Any] | None:
        if preview is None:
            return None
        return {
            "basis_size_shares": decimal_text(preview.basis_size_shares),
            "fee_rate_bps": preview.fee_rate_bps,
            "buy": self.fee_quote(preview.buy),
            "sell": self.fee_quote(preview.sell),
        }

    def fee_quote(self, quote: FeeQuote | None) -> dict[str, Any] | None:
        if quote is None:
            return None
        return {
            "price": decimal_text(quote.price),
            "price_source": quote.price_source,
            "fee_usdc": decimal_text(quote.fee_usdc),
            "fee_shares": decimal_text(quote.fee_shares),
            "charged_in": quote.charged_in,
        }

    def market_orderbook(
        self,
        *,
        token_id: str,
        condition_id: str | None,
        market_slug: str | None,
        orderbook: OrderbookSnapshot,
        source: str,
    ) -> dict[str, Any]:
        payload = self.orderbook(orderbook) or {}
        payload["token_id"] = token_id
        payload["condition_id"] = condition_id if condition_id is not None else payload.get("condition_id")
        payload["market_slug"] = market_slug if market_slug is not None else payload.get("market_slug")
        return {
            "token_id": token_id,
            "condition_id": condition_id,
            "market_slug": market_slug,
            "source": source,
            "orderbook": payload,
        }

    def market_midpoint(
        self,
        *,
        token_id: str,
        condition_id: str | None,
        market_slug: str | None,
        midpoint: Decimal | None,
        orderbook: OrderbookSnapshot | None,
        source: str,
    ) -> dict[str, Any]:
        return {
            "token_id": token_id,
            "condition_id": condition_id,
            "market_slug": market_slug,
            "source": source,
            "midpoint": decimal_text(midpoint),
            "best_bid": None if orderbook is None else decimal_text(orderbook.best_bid),
            "best_ask": None if orderbook is None else decimal_text(orderbook.best_ask),
            "last_trade_price": None if orderbook is None else decimal_text(orderbook.last_trade_price),
            "spread": None if orderbook is None else decimal_text(orderbook.spread),
            "received_at": None if orderbook is None else jsonable(orderbook.received_at),
        }

    def market_prices_history(
        self,
        *,
        token_id: str,
        history: ClobPriceHistoryDTO,
        interval: str | None,
        fidelity: int | None,
    ) -> dict[str, Any]:
        return {
            "token_id": token_id,
            "interval": interval,
            "fidelity": fidelity,
            "history": [
                {
                    "timestamp": jsonable(point.timestamp),
                    "price": decimal_text(point.price),
                }
                for point in history.history
            ],
        }

    def orderbook(self, orderbook: OrderbookSnapshot | None) -> dict[str, Any] | None:
        if orderbook is None:
            return None
        import datetime as _dt
        now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
        received_ms = int(orderbook.received_at.timestamp() * 1000)
        # bids 按价位降序、asks 按升序排好后取 top-5：方便盯盘看"真深度阶梯"
        # 区分 MM 假墙（远端 0.99/0.01 单档极大 size）和真买卖压（近端多档梯度）。
        sorted_bids = sorted(orderbook.bids, key=lambda l: l.price, reverse=True)
        sorted_asks = sorted(orderbook.asks, key=lambda l: l.price)
        top_5_bids = [
            {"price": decimal_text(level.price), "size": decimal_text(level.size)}
            for level in sorted_bids[:5]
        ]
        top_5_asks = [
            {"price": decimal_text(level.price), "size": decimal_text(level.size)}
            for level in sorted_asks[:5]
        ]
        return {
            "token_id": orderbook.token_id,
            "condition_id": orderbook.condition_id,
            "market_slug": orderbook.market_slug,
            "best_bid": decimal_text(orderbook.best_bid),
            "best_ask": decimal_text(orderbook.best_ask),
            "best_bid_size": decimal_text(orderbook.best_bid_size),
            "best_ask_size": decimal_text(orderbook.best_ask_size),
            "last_trade_price": decimal_text(orderbook.last_trade_price),
            "tick_size": decimal_text(orderbook.tick_size),
            "spread": decimal_text(orderbook.spread),
            "microprice": decimal_text(orderbook.microprice),
            "bid_liquidity_state": orderbook.bid_liquidity_state.value,
            "ask_liquidity_state": orderbook.ask_liquidity_state.value,
            "sell_actionable": orderbook.sell_actionable,
            "buy_actionable": orderbook.buy_actionable,
            "received_at": jsonable(orderbook.received_at),
            "snapshot_age_ms": now_ms - received_ms,
            "top_5_bids": top_5_bids,
            "top_5_asks": top_5_asks,
            "bids": [{"price": decimal_text(level.price), "size": decimal_text(level.size)} for level in orderbook.bids],
            "asks": [{"price": decimal_text(level.price), "size": decimal_text(level.size)} for level in orderbook.asks],
        }

    def position(self, position: Position) -> dict[str, Any]:
        # 实时取 orderbook 算流动性状态，让盯盘端一眼识别"被困持仓"：
        # FLOOR_BID_ONLY / NO_BID / DUST_BID = 当前无真实退出通道（cv 应 ≈ 0），
        # OK = 有真买盘可挂 SELL 兑现。
        liquidity_state: str | None = None
        microprice_text: str | None = None
        if position.token_id:
            try:
                ob = self.market_ws_snapshot(position.token_id)
            except Exception:
                ob = None
            if ob is not None:
                liquidity_state = ob.bid_liquidity_state.value
                if ob.microprice is not None:
                    microprice_text = decimal_text(ob.microprice)
        return {
            "condition_id": position.condition_id,
            "token_id": position.token_id,
            "outcome": self.resolve_outcome_name(
                condition_id=position.condition_id,
                token_id=position.token_id,
                market_slug=position.market_slug,
            ),
            "market_slug": position.market_slug,
            "event_slug": self.market_event_slug(
                condition_id=position.condition_id,
                token_id=position.token_id,
                market_slug=position.market_slug,
            ),
            "shares": decimal_text(position.shares),
            "cost_usdc": decimal_text(position.cost_usdc),
            "open_buy_shares": decimal_text(position.open_buy_shares),
            "open_sell_shares": decimal_text(position.open_sell_shares),
            "pending_buy_shares": decimal_text(position.pending_buy_shares),
            "confirmed_shares": decimal_text(position.confirmed_shares),
            "last_order_id": position.last_order_id,
            "last_trade_id": position.last_trade_id,
            "confirmation_status": position.confirmation_status,
            "updated_at": jsonable(position.updated_at),
            "avg_price": decimal_text(position.avg_price),
            "initial_value": decimal_text(position.initial_value),
            "current_value": decimal_text(position.current_value),
            "cash_pnl": decimal_text(position.cash_pnl),
            "percent_pnl": decimal_text(position.percent_pnl),
            "realized_pnl": decimal_text(position.realized_pnl),
            "percent_realized_pnl": decimal_text(position.percent_realized_pnl),
            "cur_price": decimal_text(position.cur_price),
            "redeemable": position.redeemable,
            "lifecycle_stage": classify_position_lifecycle(
                shares=position.shares,
                open_buy_shares=position.open_buy_shares,
                open_sell_shares=position.open_sell_shares,
                confirmed_shares=position.confirmed_shares,
            ).value,
            "bid_liquidity_state": liquidity_state,
            "microprice": microprice_text,
        }

    def order(self, order: Order) -> dict[str, Any]:
        return {
            "trace_id": order.trace_id,
            "condition_id": order.condition_id,
            "token_id": order.token_id,
            "outcome": self.resolve_outcome_name(
                condition_id=order.condition_id,
                token_id=order.token_id,
                market_slug=order.market_slug,
            ),
            "market_slug": order.market_slug,
            "event_slug": self.market_event_slug(
                condition_id=order.condition_id,
                token_id=order.token_id,
                market_slug=order.market_slug,
            ),
            "side": order.side.value,
            "order_type": order.order_type.value,
            "price": decimal_text(order.price),
            "amount_usdc": decimal_text(order.amount_usdc),
            "size_shares": decimal_text(order.size_shares),
            "filled_shares": decimal_text(order.filled_shares),
            "remaining_shares": decimal_text(order.remaining_shares),
            "notional_usdc": decimal_text(order.notional_usdc),
            "order_id": order.order_id,
            "trade_id": order.trade_id,
            "status": order.status.value,
            "idempotency_key": order.idempotency_key,
            "reason": order.reason,
            "post_only": order.post_only,
            "created_at": jsonable(order.created_at),
            "updated_at": jsonable(order.updated_at),
        }

    def fill(self, fill: Fill) -> dict[str, Any]:
        return {
            "trace_id": fill.trace_id,
            "event_type": str(fill.event_type),
            "event_id": fill.event_id,
            "market_slug": fill.market_slug,
            "event_slug": self.market_event_slug(
                condition_id=fill.condition_id,
                token_id=fill.token_id,
                market_slug=fill.market_slug,
            ),
            "condition_id": fill.condition_id,
            "token_id": fill.token_id,
            "outcome": self.resolve_outcome_name(
                condition_id=fill.condition_id,
                token_id=fill.token_id,
                market_slug=fill.market_slug,
            ),
            "reason": fill.reason,
            "created_at": jsonable(fill.created_at),
            "order_id": fill.order_id,
            "trade_id": fill.trade_id,
            "side": fill.side,
            "price": decimal_text(fill.price),
            "size": decimal_text(fill.size),
            "notional_usdc": decimal_text(fill.notional_usdc),
            "status": fill.status,
            "confirmed_at": jsonable(fill.confirmed_at),
        }

    def audit_event(self, event: AuditEvent) -> dict[str, Any]:
        return {
            "trace_id": event.trace_id,
            "event_id": event.event_id,
            "event_title": event.event_title,
            "market_slug": event.market_slug,
            "event_slug": event.event_slug,
            "condition_id": event.condition_id,
            "token_id": event.token_id,
            "outcome": event.outcome,
            "side": event.side,
            "order_type": event.order_type,
            "price": jsonable(event.price),
            "size": jsonable(event.size),
            "notional_usdc": jsonable(event.notional_usdc),
            "order_id": event.order_id,
            "trade_id": event.trade_id,
            "tx_hash": event.tx_hash,
            "status": event.status,
            "reason": event.reason,
            "raw_response": event.raw_response,
            "payload": _normalize_token_id_strings(jsonable(event.payload)),
            "created_at": jsonable(event.created_at),
            "updated_at": jsonable(event.updated_at),
        }

    def outbox_event(self, event: OutboxEvent) -> dict[str, Any]:
        return {
            "trace_id": event.trace_id,
            "event_type": event.event_type,
            "idempotency_key": event.idempotency_key,
            "event_id": event.event_id,
            "market_slug": event.market_slug,
            "event_slug": event.event_slug,
            "condition_id": event.condition_id,
            "token_id": event.token_id,
            "reason": event.reason,
            "created_at": jsonable(event.created_at),
            "priority": event.priority,
            "retry_count": event.retry_count,
            "last_error": event.last_error,
            "raw_response_summary": event.raw_response_summary,
            "payload": jsonable(event.payload),
        }

    def allocation(self, allocation: Allocation) -> dict[str, Any]:
        return {
            "condition_id": allocation.condition_id,
            "market_slug": allocation.market_slug,
            "token_id": allocation.token_id,
            "event_slug": self.market_event_slug(
                condition_id=allocation.condition_id,
                token_id=allocation.token_id,
                market_slug=allocation.market_slug,
            ),
            "outcome": self.resolve_outcome_name(
                condition_id=allocation.condition_id,
                token_id=allocation.token_id,
                market_slug=allocation.market_slug,
            ),
            "target_budget_usdc": decimal_text(allocation.target_budget_usdc),
            "buy_budget_usdc": decimal_text(allocation.buy_budget_usdc),
            "current_exposure_usdc": decimal_text(allocation.current_exposure_usdc),
            "released_budget_usdc": decimal_text(allocation.released_budget_usdc),
            "reason": allocation.reason,
            "release_reason": allocation.release_reason,
            "idempotency_key": allocation.idempotency_key,
        }

    def resolve_outcome_name(
        self,
        *,
        condition_id: str | None,
        token_id: str | None,
        market_slug: str | None = None,
    ) -> str | None:
        if token_id is None:
            return None
        registry = self.registry_snapshot_provider()
        market = None
        if condition_id is not None:
            market = registry.get_by_condition_id(condition_id)
        if market is None:
            market = registry.get_by_token_id(token_id)
        if market is None and market_slug is not None:
            market = registry.get_by_slug(market_slug)
        if market is None:
            return None
        outcome = market.get_outcome_by_token_id(token_id)
        return None if outcome is None else outcome.outcome

    def market_event_slug(
        self,
        *,
        condition_id: str | None,
        token_id: str | None,
        market_slug: str | None,
    ) -> str | None:
        registry = self.registry_snapshot_provider()
        market = None
        if condition_id is not None:
            market = registry.get_by_condition_id(condition_id)
        if market is None and token_id is not None:
            market = registry.get_by_token_id(token_id)
        if market is None and market_slug is not None:
            market = registry.get_by_slug(market_slug)
        return None if market is None else market.event_slug

    def review(self, review: TradingReviewResult) -> dict[str, Any]:
        return {
            "operation": review.operation,
            "submitted": review.submitted,
            "risk_decision": None
            if review.risk_decision is None
            else {
                "passed": review.risk_decision.passed,
                "reason": review.risk_decision.reason,
                "retryable": review.risk_decision.retryable,
            },
            "order_result": self.order_result(review.order_result),
            "submission_error": review.submission_error,
        }

    def order_result(self, result: OrderResult | None) -> dict[str, Any] | None:
        if result is None:
            return None
        return {
            "trace_id": result.trace_id,
            "condition_id": result.condition_id,
            "token_id": result.token_id,
            "outcome": self.resolve_outcome_name(
                condition_id=result.condition_id,
                token_id=result.token_id,
                market_slug=result.market_slug,
            ),
            "market_slug": result.market_slug,
            "status": result.status.value,
            "order_id": result.order_id,
            "trade_id": result.trade_id,
            "side": None if result.side is None else result.side.value,
            "order_type": None if result.order_type is None else result.order_type.value,
            "price": decimal_text(result.price),
            "requested_amount_usdc": decimal_text(result.requested_amount_usdc),
            "requested_size_shares": decimal_text(result.requested_size_shares),
            "matched_shares": decimal_text(result.matched_shares),
            "remaining_shares": decimal_text(result.remaining_shares),
            "spent_usdc": decimal_text(result.spent_usdc),
            "notional_usdc": decimal_text(result.notional_usdc),
            "reason": result.reason,
            "retryable": result.retryable,
            "raw_response_summary": result.raw_response_summary,
            "timestamps": jsonable(result.timestamps),
        }

    def reconcile_result(self, result: Any) -> dict[str, Any]:
        plan: ReconcilePlan = result.plan
        return {
            "trace_id": result.trace_id,
            "status": "ok",
            "plan": {
                "trace_id": plan.trace_id,
                "generated_at": jsonable(plan.generated_at),
                "total_actions": plan.total_actions,
                "paused_market_count": plan.paused_market_count,
                "diff_count": plan.diff_count,
                "has_changes": plan.has_changes,
                "market_plans": [
                    {
                        "trace_id": market_plan.trace_id,
                        "market": self.market(market_plan.market),
                        "actions": [self.reconcile_action(action) for action in market_plan.actions],
                        "pause_trading": market_plan.pause_trading,
                        "pause_reason": market_plan.pause_reason,
                    }
                    for market_plan in plan.market_plans
                ],
            },
            "applied_actions": [self.reconcile_action(action) for action in result.applied_actions],
            "failed_actions": [
                {
                    "action": self.reconcile_action(action),
                    "reason": reason,
                }
                for action, reason in result.failed_actions
            ],
        }

    def reconcile_action(self, action: ReconcileAction) -> dict[str, Any]:
        return {
            "action_type": action.action_type.value,
            "trace_id": action.trace_id,
            "condition_id": action.condition_id,
            "token_id": action.token_id,
            "market_slug": action.market_slug,
            "reason": action.reason,
            "source_order_id": action.source_order_id,
            "source_order_side": None if action.source_order_side is None else action.source_order_side.value,
            "target_size_shares": decimal_text(action.target_size_shares),
            "target_notional_usdc": decimal_text(action.target_notional_usdc),
            "pause_reason": action.pause_reason,
            "metadata": jsonable(action.metadata),
        }
