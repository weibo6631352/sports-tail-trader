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


# Polymarket 钱包对 Exchange 的 ERC20 approval 通常是 max(uint256) ≈ 1.16e77，
# 远超 ``account_snapshots.allowance_usdc NUMERIC(38, 18)``（max ≈ 1e20）。
# Kelly 仅用 ``min(balance, allowance)``，超大 allowance 等价"effectively unlimited"，
# 在内存与持久化两层都 cap 到 1e18（≫ Polymarket 全市场 TVL，足够任何实际下单）。
_ALLOWANCE_CAP_USDC = Decimal("1000000000000000000")  # 1e18


class AccountStateStore:
    """Maintains copy-on-write account snapshots for P0 readers.

    并发模型：``snapshot()`` 是 **lock-free**（直接返回 ``self._snapshot`` 引用，
    CPython GIL 保证读到的是完整旧/新快照而非撕裂状态）。``_lock`` 只守护写路径
    之间的 read-modify-write（防止丢失更新）。

    §7 不变量：P0 决策热路径只调 ``snapshot()`` 读取——写入 AccountStateStore
    的都是后台路径（user_ws / reconcile / fills），所以写锁竞争永远不会反向
    阻塞 P0 主链路。若日后引入 P0 写入路径，必须先拆分片锁。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._positions: dict[tuple[str, str], Position] = {}
        self._open_orders: dict[str, Order] = {}
        self._fills: dict[str, Fill] = {}
        self._balance_usdc = Decimal("0")
        self._allowance_usdc = Decimal("0")
        # peak_bankroll_usdc 单调维护：取 effective_bankroll = min(balance, allowance) -
        # open_buy_reserved_usdc，每次刷新时取 max。Kelly drawdown lockout 用此基准。
        self._peak_bankroll_usdc = Decimal("0")
        self._user_ws_connected = False
        self._allow_new_entries = False
        self._market_pauses: dict[str, MarketPause] = {}
        self._last_reconcile_at: datetime | None = None
        self._snapshot = AccountSnapshot()

    def snapshot(self) -> AccountSnapshot:
        return self._snapshot

    def restore_peak_bankroll(self, peak_usdc: Decimal) -> None:
        """重启时从最新 account_snapshots 行恢复历史 peak。

        ``_publish_snapshot_locked`` 用 ``max(loaded_peak, current)`` 维护单调，
        所以这里只设 in-memory 起始值；下一次 ``update_balances`` 会触发 publish
        并重算 peak。仅在启动期被 main.py 调用一次。
        """

        with self._lock:
            if peak_usdc > self._peak_bankroll_usdc:
                self._peak_bankroll_usdc = peak_usdc

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
                # Cap 到 NUMERIC(38, 18) 可表示的"effectively unlimited"——见模块顶部
                # ``_ALLOWANCE_CAP_USDC`` 注释。max-uint256 approval 是常见情况。
                self._allowance_usdc = (
                    min(allowance_usdc, _ALLOWANCE_CAP_USDC)
                    if allowance_usdc > _ALLOWANCE_CAP_USDC
                    else allowance_usdc
                )
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
        # peak_bankroll_usdc 单调上升——drawdown lockout 把 peak 当作历史最高水位。
        # 锚定在 **available_usdc**（实际可调用现金），不含 position MTM。
        # 原设计加 Σposition.current_value 会把浮盈推上 peak（实测 balance $140 时
        # peak 涨到 $296），等浮盈兑现成实际亏损后 balance 跌回 $112 但 peak 单调
        # 不降 → equity < peak × halt_fraction 永久误锁新入场（drawdown_lockout_active），
        # 与 §17 "宁可输一笔不要系统性放弃"哲学冲突。
        # 真实"曾经拿到的钱"上沿只有 USDC 现金，浮盈不是已实现资金不应进 peak。
        provisional = AccountSnapshot(
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
        current_equity = provisional.available_usdc
        if current_equity > self._peak_bankroll_usdc:
            self._peak_bankroll_usdc = current_equity
        snapshot = replace(provisional, peak_bankroll_usdc=self._peak_bankroll_usdc)
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
