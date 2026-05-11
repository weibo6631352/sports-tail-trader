from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any, Mapping, Sequence

from polymarket_trader.domain.account import AccountSnapshot, MarketPause
from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import Order, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: Any | None, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return default


def _int(value: Any | None, default: int | None = None) -> int | None:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        try:
            return int(Decimal(text))
        except (InvalidOperation, ValueError):
            return default


def _bool(value: Any | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "open", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "closed", "disabled"}:
        return False
    return default


def _bool_or_none(value: Any | None) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"1", "true", "yes", "y", "open", "enabled"}:
        return True
    if lowered in {"0", "false", "no", "n", "closed", "disabled"}:
        return False
    return None


def _datetime(value: Any | None, default: datetime | None = None) -> datetime | None:
    if value is None:
        return default
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return default
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return default
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fee_rate_units_from_market_payload(payload: Mapping[str, Any] | None) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    fee_schedule = payload.get("feeSchedule")
    if not isinstance(fee_schedule, Mapping):
        fee_schedule = payload.get("fee_schedule")
    if not isinstance(fee_schedule, Mapping):
        return None
    rate = fee_schedule.get("rate")
    if rate is None:
        rate = fee_schedule.get("base_fee")
    if rate is None:
        rate = fee_schedule.get("baseFee")
    if rate is None or isinstance(rate, bool):
        return None
    numeric = _decimal(rate)
    if numeric is None or numeric < Decimal("0"):
        return None
    if numeric < Decimal("1"):
        return int((numeric * Decimal("1000")).to_integral_value())
    if numeric == numeric.to_integral_value():
        return int(numeric)
    return int((numeric * Decimal("1000")).to_integral_value())


def _string_tuple(value: Any | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        items = []
        for item in value:
            text = _text(item)
            if text:
                items.append(text)
        return tuple(items)
    text = _text(value)
    return () if text is None else (text,)


def _mapping(value: Any | None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _market_pauses_from_record(market_pauses: Any | None) -> tuple[MarketPause, ...]:
    if not isinstance(market_pauses, (list, tuple)):
        return ()
    pauses: list[MarketPause] = []
    for item in market_pauses:
        if not isinstance(item, Mapping):
            continue
        condition_id = _text(item.get("condition_id"))
        reason = _text(item.get("reason"))
        source = _text(item.get("source"))
        recoverable = _bool_or_none(item.get("recoverable"))
        if condition_id is None or reason is None or source is None or recoverable is None:
            continue
        try:
            pauses.append(
                MarketPause.build(
                    condition_id=condition_id,
                    reason=reason,
                    source=source,
                    recoverable=recoverable,
                )
            )
        except ValueError:
            continue
    return tuple(pauses)


def _price_levels(value: Any | None) -> tuple[PriceLevel, ...]:
    if not isinstance(value, list):
        return ()
    levels: list[PriceLevel] = []
    for item in value:
        if isinstance(item, Mapping):
            price = _decimal(item.get("price"))
            size = _decimal(item.get("size"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price = _decimal(item[0])
            size = _decimal(item[1])
        else:
            continue
        if price is None or size is None:
            continue
        levels.append(PriceLevel(price=price, size=size))
    return tuple(levels)


def _trading_status(value: Any | None) -> TradingStatus:
    text = (_text(value) or TradingStatus.CANDIDATE.value).lower()
    try:
        return TradingStatus(text)
    except ValueError:
        return TradingStatus.CANDIDATE


def _order_side(value: Any | None) -> OrderSide | None:
    text = (_text(value) or "").upper()
    try:
        return OrderSide(text)
    except ValueError:
        return None


def _order_type(value: Any | None) -> OrderType | None:
    text = (_text(value) or "").upper()
    try:
        return OrderType(text)
    except ValueError:
        return None


def _order_status(value: Any | None) -> OrderStatus:
    text = (_text(value) or "").lower()
    mapping = {
        "created": OrderStatus.CREATED,
        "signed": OrderStatus.SIGNED,
        "submitted": OrderStatus.SUBMITTED,
        "cancel_requested": OrderStatus.CANCEL_REQUESTED,
        "matched": OrderStatus.MATCHED,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "partial_fill": OrderStatus.PARTIALLY_FILLED,
        "no_fill": OrderStatus.NO_FILL,
        "live": OrderStatus.LIVE,
        "cancelled": OrderStatus.CANCELLED,
        "canceled": OrderStatus.CANCELLED,
        "rejected": OrderStatus.REJECTED,
        "failed": OrderStatus.FAILED,
        "full_fill": OrderStatus.MATCHED,
        "unknown_timeout": OrderStatus.FAILED,
        "timeout": OrderStatus.FAILED,
    }
    return mapping.get(text, OrderStatus.FAILED)


def _log_skip(kind: str, record: Mapping[str, Any], reason: str) -> None:
    logger.debug(
        "skip persistence record because required fields are missing",
        extra={
            "kind": kind,
            "reason": reason,
            "trace_id": _text(record.get("trace_id")),
            "event_id": _text(record.get("event_id")),
        },
    )


def audit_event_from_record(record: Mapping[str, Any]) -> AuditEvent | None:
    trace_id = _text(record.get("trace_id"))
    event_title = _text(record.get("event_title"))
    strategy_id = _text(record.get("strategy_id"))
    if trace_id is None or event_title is None or strategy_id is None:
        _log_skip("audit", record, "missing trace_id/event_title/strategy_id")
        return None
    created_at = _datetime(record.get("created_at"), _utc_now())
    payload = dict(record)
    payload["created_at"] = created_at
    payload["updated_at"] = _datetime(record.get("updated_at"), created_at) or created_at
    return AuditEvent(
        event_title=event_title,
        trace_id=trace_id,
        strategy_id=strategy_id,
        created_at=created_at,
        raw_response=record.get("raw_response"),
        event_slug=_text(record.get("event_slug")),
        payload=payload,
    )


def market_from_record(record: Mapping[str, Any]) -> Market | None:
    condition_id = _text(record.get("condition_id"))
    market_slug = _text(record.get("market_slug"))
    raw_market = record.get("raw_payload")
    if not isinstance(raw_market, Mapping):
        raw_market = {}
    outcomes = _market_outcomes(
        record.get("outcomes") or raw_market.get("outcomes"),
        record.get("token_ids") or raw_market.get("token_ids"),
    )
    if condition_id is None or market_slug is None or not outcomes:
        _log_skip("market", record, "missing condition_id/market_slug/outcomes")
        return None
    schedule_fee_rate_bps = _fee_rate_units_from_market_payload(raw_market)
    return Market(
        condition_id=condition_id,
        market_slug=market_slug,
        outcomes=outcomes,
        event_id=_text(record.get("event_id")),
        event_title=_text(record.get("event_title")),
        event_slug=_text(record.get("event_slug")),
        icon_url=_text(record.get("icon_url")) or _text(raw_market.get("icon_url")) or _text(raw_market.get("icon")),
        end_date=(
            _datetime(record.get("end_date"))
            or _datetime(raw_market.get("end_date"))
            or _datetime(raw_market.get("endDate"))
        ),
        game_start_time=(
            _datetime(record.get("game_start_time"))
            or _datetime(raw_market.get("game_start_time"))
            or _datetime(raw_market.get("gameStartTime"))
            or _datetime(raw_market.get("gameStart"))
        ),
        tick_size=_decimal(record.get("tick_size"), Decimal("0.01")) or Decimal("0.01"),
        min_order_size=_decimal(record.get("min_order_size"), Decimal("1")) or Decimal("1"),
        neg_risk=_bool(record.get("neg_risk")),
        fees_enabled=None if record.get("fees_enabled") is None else _bool(record.get("fees_enabled")),
        maker_base_fee_bps=_int(record.get("maker_base_fee_bps")),
        taker_base_fee_bps=(
            schedule_fee_rate_bps
            if schedule_fee_rate_bps is not None
            else _int(record.get("taker_base_fee_bps"))
        ),
        fee_rate_bps=(
            schedule_fee_rate_bps
            if schedule_fee_rate_bps is not None
            else _int(record.get("fee_rate_bps"))
        ),
        fee_rate_updated_at=(
            None
            if schedule_fee_rate_bps is not None
            else _datetime(record.get("fee_rate_updated_at"))
        ),
        category=_text(record.get("category")),
        tags=_string_tuple(record.get("tags")),
        matched_keywords=_string_tuple(record.get("matched_keywords")),
        trading_status=_trading_status(record.get("trading_status")),
        reject_reason=_text(record.get("reject_reason")),
    )


def _market_outcomes(
    outcome_records: Any | None,
    token_ids_value: Any | None,
) -> tuple[MarketOutcome, ...]:
    outcomes: list[MarketOutcome] = []
    if isinstance(outcome_records, Sequence) and not isinstance(outcome_records, (str, bytes, bytearray)):
        for item in outcome_records:
            if not isinstance(item, Mapping):
                continue
            token_id = _text(item.get("token_id"))
            outcome = _text(item.get("outcome"))
            if token_id is None or outcome is None:
                continue
            outcomes.append(MarketOutcome(token_id=token_id, outcome=outcome))
    if outcomes:
        return tuple(outcomes)
    token_ids = _string_tuple(token_ids_value)
    if not token_ids:
        return tuple()
    outcome_names = ("YES", "NO") if len(token_ids) == 2 else tuple(
        f"OUTCOME_{index}" for index in range(len(token_ids))
    )
    return tuple(
        MarketOutcome(token_id=token_id, outcome=outcome_names[index])
        for index, token_id in enumerate(token_ids)
    )


def orderbook_from_record(record: Mapping[str, Any]) -> OrderbookSnapshot | None:
    token_id = _text(record.get("token_id"))
    if token_id is None:
        _log_skip("orderbook", record, "missing token_id")
        return None
    return OrderbookSnapshot(
        token_id=token_id,
        best_bid=_decimal(record.get("best_bid")),
        best_ask=_decimal(record.get("best_ask")),
        bids=_price_levels(record.get("bids")),
        asks=_price_levels(record.get("asks")),
        received_at=_datetime(
            record.get("received_at") or record.get("snapshot_time") or record.get("created_at"),
            _utc_now(),
        )
        or _utc_now(),
        market_slug=_text(record.get("market_slug")),
        condition_id=_text(record.get("condition_id")),
        best_bid_size=_decimal(record.get("best_bid_size")),
        best_ask_size=_decimal(record.get("best_ask_size")),
        last_trade_price=_decimal(record.get("last_trade_price")),
        tick_size=_decimal(record.get("tick_size")),
    )


def order_from_record(record: Mapping[str, Any]) -> Order | None:
    condition_id = _text(record.get("condition_id"))
    token_id = _text(record.get("token_id"))
    side = _order_side(record.get("side"))
    order_type = _order_type(record.get("order_type"))
    price = _decimal(record.get("price"))
    strategy_id = _text(record.get("strategy_id"))
    if condition_id is None or token_id is None or side is None or order_type is None or price is None or strategy_id is None:
        _log_skip("order", record, "missing condition_id/token_id/side/order_type/price/strategy_id")
        return None
    return Order(
        strategy_id=strategy_id,
        trace_id=_text(record.get("trace_id")) or "",
        condition_id=condition_id,
        token_id=token_id,
        market_slug=_text(record.get("market_slug")),
        side=side,
        order_type=order_type,
        price=price,
        amount_usdc=_decimal(record.get("amount_usdc")),
        size_shares=_decimal(record.get("size_shares")),
        filled_shares=_decimal(record.get("filled_shares"), Decimal("0")) or Decimal("0"),
        remaining_shares=_decimal(record.get("remaining_shares")),
        notional_usdc=_decimal(record.get("notional_usdc")),
        order_id=_text(record.get("order_id")),
        trade_id=_text(record.get("trade_id")),
        status=_order_status(record.get("status")),
        idempotency_key=_text(record.get("idempotency_key")),
        reason=_text(record.get("reason")) or "",
        post_only=_bool(record.get("post_only")),
        created_at=_datetime(record.get("created_at")),
        updated_at=_datetime(record.get("updated_at") or record.get("created_at")),
    )


def fill_from_record(record: Mapping[str, Any]) -> Fill | None:
    trace_id = _text(record.get("trace_id"))
    event_type = _text(record.get("event_type"))
    strategy_id = _text(record.get("strategy_id"))
    if trace_id is None or event_type is None or strategy_id is None:
        _log_skip("fill", record, "missing trace_id/event_type/strategy_id")
        return None
    return Fill(
        strategy_id=strategy_id,
        trace_id=trace_id,
        event_type=event_type,
        event_id=_text(record.get("event_id")) or "",
        market_slug=_text(record.get("market_slug")),
        condition_id=_text(record.get("condition_id")),
        token_id=_text(record.get("token_id")),
        created_at=_datetime(record.get("created_at"), _utc_now()) or _utc_now(),
        order_id=_text(record.get("order_id")),
        trade_id=_text(record.get("trade_id")),
        side=_text(record.get("side")),
        price=_decimal(record.get("price")),
        size=_decimal(record.get("size")),
        notional_usdc=_decimal(record.get("notional_usdc")),
        status=_text(record.get("status")) or "confirmed",
        confirmed_at=_datetime(record.get("confirmed_at") or record.get("created_at")),
    )


def position_from_record(record: Mapping[str, Any]) -> Position | None:
    condition_id = _text(record.get("condition_id"))
    token_id = _text(record.get("token_id"))
    strategy_id = _text(record.get("strategy_id"))
    if condition_id is None or token_id is None or strategy_id is None:
        _log_skip("position", record, "missing condition_id/token_id/strategy_id")
        return None
    return Position(
        strategy_id=strategy_id,
        condition_id=condition_id,
        token_id=token_id,
        shares=_decimal(record.get("shares"), Decimal("0")) or Decimal("0"),
        cost_usdc=_decimal(record.get("cost_usdc"), Decimal("0")) or Decimal("0"),
        market_slug=_text(record.get("market_slug")),
        open_buy_shares=_decimal(record.get("open_buy_shares"), Decimal("0")) or Decimal("0"),
        open_sell_shares=_decimal(record.get("open_sell_shares"), Decimal("0")) or Decimal("0"),
        pending_buy_shares=_decimal(record.get("pending_buy_shares"), Decimal("0")) or Decimal("0"),
        confirmed_shares=_decimal(record.get("confirmed_shares"), Decimal("0")) or Decimal("0"),
        last_order_id=_text(record.get("last_order_id")),
        last_trade_id=_text(record.get("last_trade_id")),
        confirmation_status=_text(record.get("confirmation_status")) or "unknown",
        updated_at=_datetime(record.get("updated_at") or record.get("created_at")),
        avg_price=_decimal(record.get("avg_price")),
        initial_value=_decimal(record.get("initial_value")),
        current_value=_decimal(record.get("current_value")),
        cash_pnl=_decimal(record.get("cash_pnl")),
        percent_pnl=_decimal(record.get("percent_pnl")),
        realized_pnl=_decimal(record.get("realized_pnl")),
        percent_realized_pnl=_decimal(record.get("percent_realized_pnl")),
        cur_price=_decimal(record.get("cur_price")),
        redeemable=_bool(record.get("redeemable"), False) if record.get("redeemable") is not None else None,
    )


def account_snapshot_from_record(record: Mapping[str, Any]) -> AccountSnapshot | None:
    balance_usdc = _decimal(record.get("balance_usdc"))
    allowance_usdc = _decimal(record.get("allowance_usdc"))
    if balance_usdc is None or allowance_usdc is None:
        _log_skip("account", record, "missing balance_usdc/allowance_usdc")
        return None
    return AccountSnapshot(
        balance_usdc=balance_usdc,
        allowance_usdc=allowance_usdc,
        user_ws_connected=_bool(record.get("user_ws_connected"), False),
        allow_new_entries=_bool(record.get("allow_new_entries"), False),
        market_pauses=_market_pauses_from_record(record.get("market_pauses")),
        last_reconcile_at=_datetime(record.get("last_reconcile_at")),
    )


def allocation_from_record(record: Mapping[str, Any]) -> Allocation | None:
    condition_id = _text(record.get("condition_id"))
    strategy_id = _text(record.get("strategy_id"))
    if condition_id is None or strategy_id is None:
        _log_skip("allocation", record, "missing condition_id/strategy_id")
        return None
    return Allocation(
        strategy_id=strategy_id,
        condition_id=condition_id,
        target_budget_usdc=_decimal(record.get("target_budget_usdc"), Decimal("0")) or Decimal("0"),
        buy_budget_usdc=_decimal(record.get("buy_budget_usdc"), Decimal("0")) or Decimal("0"),
        market_slug=_text(record.get("market_slug")),
        token_id=_text(record.get("token_id")),
        current_exposure_usdc=_decimal(record.get("current_exposure_usdc"), Decimal("0")) or Decimal("0"),
        released_budget_usdc=_decimal(record.get("released_budget_usdc"), Decimal("0")) or Decimal("0"),
        reason=_text(record.get("reason")) or "",
        idempotency_key=_text(record.get("idempotency_key")),
        release_reason=_text(record.get("release_reason")) or "",
    )


def decision_record_from_record(record: Mapping[str, Any]) -> DecisionRecord | None:
    trace_id = _text(record.get("trace_id"))
    condition_id = _text(record.get("condition_id"))
    record_id = _text(record.get("record_id"))
    strategy_id = _text(record.get("strategy_id"))
    if trace_id is None or condition_id is None or record_id is None or strategy_id is None:
        _log_skip("decision", record, "missing trace_id/condition_id/record_id/strategy_id")
        return None
    decision_input = _mapping(record.get("decision_input")) or {}
    decision_output = _mapping(record.get("decision_output")) or {}
    accepted = bool(record.get("accepted") or False)
    return DecisionRecord(
        record_id=record_id,
        strategy_id=strategy_id,
        trace_id=trace_id,
        condition_id=condition_id,
        hook_name=_text(record.get("hook_name")) or "",
        token_id=_text(record.get("token_id")),
        market_slug=_text(record.get("market_slug")),
        decision_input=decision_input,
        decision_output=decision_output,
        accepted=accepted,
        reason=_text(record.get("reason")),
        created_at=_datetime(record.get("created_at"), _utc_now()) or _utc_now(),
    )


def outbox_event_from_record(record: Mapping[str, Any]) -> OutboxEvent | None:
    trace_id = _text(record.get("trace_id"))
    event_type = _text(record.get("event_type"))
    idempotency_key = _text(record.get("idempotency_key"))
    if trace_id is None or event_type is None or idempotency_key is None:
        _log_skip("outbox", record, "missing trace_id/event_type/idempotency_key")
        return None
    payload = _mapping(record.get("payload")) or {}
    return OutboxEvent(
        trace_id=trace_id,
        event_type=event_type,
        idempotency_key=idempotency_key,
        event_id=_text(record.get("event_id")) or "",
        market_slug=_text(record.get("market_slug")),
        event_slug=_text(record.get("event_slug")),
        condition_id=_text(record.get("condition_id")),
        token_id=_text(record.get("token_id")),
        reason=_text(record.get("reason")),
        created_at=_datetime(record.get("created_at"), _utc_now()) or _utc_now(),
        priority=int(record.get("priority", 0)),
        retry_count=int(record.get("retry_count", 0)),
        last_error=_text(record.get("last_error")),
        raw_response_summary=_text(record.get("raw_response_summary")),
        payload=payload,
    )
