from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock

from polymarket_trader.domain.account import AccountSnapshot, MarketPause, MarketPauseSource
from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.position import Position


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AccountStateStore:
    """Maintains copy-on-write account snapshots for P0 readers."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._positions: dict[tuple[str, str], Position] = {}
        self._open_orders: dict[str, Order] = {}
        self._fills: dict[str, Fill] = {}
        self._balance_usdc = Decimal("0")
        self._allowance_usdc = Decimal("0")
        self._user_ws_connected = False
        self._allow_new_entries = False
        self._market_pauses: dict[str, MarketPause] = {}
        self._last_reconcile_at: datetime | None = None
        self._snapshot = AccountSnapshot()

    def snapshot(self) -> AccountSnapshot:
        return self._snapshot

    def update_balances(
        self,
        *,
        balance_usdc: Decimal | None = None,
        allowance_usdc: Decimal | None = None,
    ) -> AccountSnapshot:
        with self._lock:
            if balance_usdc is not None:
                self._balance_usdc = balance_usdc
            if allowance_usdc is not None:
                self._allowance_usdc = allowance_usdc
            return self._publish_snapshot_locked()

    def upsert_position(self, position: Position) -> AccountSnapshot:
        with self._lock:
            key = (position.condition_id, position.token_id)
            self._positions[key] = _merge_position_authority_fields(
                self._positions.get(key),
                position,
            )
            return self._publish_snapshot_locked()

    def replace_positions(self, positions: tuple[Position, ...]) -> AccountSnapshot:
        with self._lock:
            previous_positions = self._positions
            self._positions = {
                (position.condition_id, position.token_id): _merge_position_authority_fields(
                    previous_positions.get((position.condition_id, position.token_id)),
                    position,
                )
                for position in positions
            }
            return self._publish_snapshot_locked()

    def upsert_order(self, order: Order) -> AccountSnapshot:
        with self._lock:
            order_id = order.order_id or order.idempotency_key or (
                f"{order.condition_id}:{order.token_id}:{order.side.value}:{order.status.value}"
            )
            self._open_orders[order_id] = order
            return self._publish_snapshot_locked()

    def remove_order(self, order_id: str) -> AccountSnapshot:
        with self._lock:
            self._open_orders.pop(order_id, None)
            return self._publish_snapshot_locked()

    def replace_open_orders(self, orders: tuple[Order, ...]) -> AccountSnapshot:
        with self._lock:
            self._open_orders = {}
            for order in orders:
                order_id = order.order_id or order.idempotency_key or (
                    f"{order.condition_id}:{order.token_id}:{order.side.value}:{order.status.value}"
                )
                self._open_orders[order_id] = order
            return self._publish_snapshot_locked()

    def record_fill(self, fill: Fill) -> AccountSnapshot:
        with self._lock:
            self._fills[fill.event_id] = fill
            return self._publish_snapshot_locked()

    def replace_fills(self, fills: tuple[Fill, ...]) -> AccountSnapshot:
        with self._lock:
            self._fills = {fill.event_id: fill for fill in fills}
            return self._publish_snapshot_locked()

    def mark_user_ws_connected(self, connected: bool) -> AccountSnapshot:
        with self._lock:
            self._user_ws_connected = connected
            if not connected:
                self._last_reconcile_at = None
                self._allow_new_entries = False
            else:
                self._allow_new_entries = self._entry_gate_can_open_locked()
            return self._publish_snapshot_locked()

    def set_allow_new_entries(self, allowed: bool) -> AccountSnapshot:
        with self._lock:
            self._allow_new_entries = allowed and self._entry_gate_can_open_locked()
            return self._publish_snapshot_locked()

    def pause_market(
        self,
        condition_id: str,
        *,
        reason: str,
        source: MarketPauseSource | str | None = None,
        recoverable: bool | None = None,
    ) -> AccountSnapshot:
        with self._lock:
            self._market_pauses[condition_id] = MarketPause.build(
                condition_id=condition_id,
                reason=reason,
                source=source,
                recoverable=recoverable,
            )
            return self._publish_snapshot_locked()

    def resume_market(self, condition_id: str) -> AccountSnapshot:
        with self._lock:
            self._market_pauses.pop(condition_id, None)
            return self._publish_snapshot_locked()

    def mark_reconciled(self, reconciled_at: datetime | None = None) -> AccountSnapshot:
        with self._lock:
            self._last_reconcile_at = reconciled_at or _utc_now()
            self._allow_new_entries = self._entry_gate_can_open_locked()
            return self._publish_snapshot_locked()

    def _entry_gate_can_open_locked(self) -> bool:
        return self._user_ws_connected and self._last_reconcile_at is not None

    def _publish_snapshot_locked(self) -> AccountSnapshot:
        snapshot = AccountSnapshot(
            balance_usdc=self._balance_usdc,
            allowance_usdc=self._allowance_usdc,
            positions=tuple(self._positions.values()),
            open_orders=tuple(self._open_orders.values()),
            fills=tuple(self._fills.values()),
            user_ws_connected=self._user_ws_connected,
            allow_new_entries=self._allow_new_entries,
            market_pauses=tuple(self._market_pauses.values()),
            last_reconcile_at=self._last_reconcile_at,
        )
        self._snapshot = snapshot
        return snapshot


def _merge_position_authority_fields(
    previous: Position | None,
    incoming: Position,
) -> Position:
    """把 WS 稀疏持仓快照和 Data API 权威结算字段合并成单一热状态。"""

    if previous is None:
        return incoming

    return replace(
        incoming,
        market_slug=incoming.market_slug or previous.market_slug,
        avg_price=incoming.avg_price if incoming.avg_price is not None else previous.avg_price,
        initial_value=(
            incoming.initial_value if incoming.initial_value is not None else previous.initial_value
        ),
        current_value=(
            incoming.current_value if incoming.current_value is not None else previous.current_value
        ),
        cash_pnl=incoming.cash_pnl if incoming.cash_pnl is not None else previous.cash_pnl,
        percent_pnl=(
            incoming.percent_pnl if incoming.percent_pnl is not None else previous.percent_pnl
        ),
        realized_pnl=(
            incoming.realized_pnl if incoming.realized_pnl is not None else previous.realized_pnl
        ),
        percent_realized_pnl=(
            incoming.percent_realized_pnl
            if incoming.percent_realized_pnl is not None
            else previous.percent_realized_pnl
        ),
        cur_price=incoming.cur_price if incoming.cur_price is not None else previous.cur_price,
        redeemable=incoming.redeemable if incoming.redeemable is not None else previous.redeemable,
    )
