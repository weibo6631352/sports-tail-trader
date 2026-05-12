from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from polymarket_trader.domain.allocation import AllocationPlan
from polymarket_trader.domain.fees import calculate_trade_fee
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.order import Order, OrderIntent, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position

_MIN_CLOB_NOTIONAL_USDC = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class RiskCheck:
    name: str
    passed: bool
    reason: str = ""
    field: str | None = None
    value: object | None = None
    suggested_action: str = ""
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class RiskDecision:
    passed: bool
    reason: str = ""
    trace_id: str = ""
    failed_field: str | None = None
    suggested_action: str = ""
    retryable: bool = False
    checks: tuple[RiskCheck, ...] = field(default_factory=tuple)

class RiskManager:
    """Mandatory gate before any order intent reaches the executor."""

    def check_order_intent(
        self,
        intent: OrderIntent,
        *,
        market: Market | None = None,
        orderbook: OrderbookSnapshot | None = None,
        position: Position | None = None,
        open_orders: Iterable[Order] = (),
        allocation_plan: AllocationPlan | None = None,
        classification_passed: bool | None = None,
        classification_reason: str | None = None,
        market_active: bool | None = None,
        market_open: bool | None = None,
        clob_enabled: bool | None = None,
        resolved: bool | None = None,
        cancelled: bool | None = None,
        archived: bool | None = None,
        geoblocked: bool = False,
        balance_usdc: Decimal | None = None,
        allowance_usdc: Decimal | None = None,
        portfolio_total_invested_usdc: Decimal | None = None,
        open_orders_count: int | None = None,
        retry_count: int = 0,
        order_retry_limit: int | None = None,
        # Kelly 风控参数：bankroll 来自 ``min(account.available, settings.portfolio_budget)``；
        # max_position_fraction 替代旧 max_order/market/total_usdc 的三层硬上限；
        # round_up_max_overbet_ratio 同步放宽 RiskManager 的 effective cap，避免
        # 与 Kelly 引擎的 round-up 路径不一致；peak / drawdown_halt 实现 §5 drawdown lockout。
        bankroll_usdc: Decimal | None = None,
        kelly_max_position_fraction: Decimal | None = None,
        kelly_round_up_max_overbet_ratio: Decimal | None = None,
        peak_bankroll_usdc: Decimal | None = None,
        kelly_drawdown_halt_fraction: Decimal | None = None,
        min_order_size: Decimal | None = None,
    ) -> RiskDecision:
        # 这里只读本地快照和热状态；P0 路径不允许为了下单临时打 REST 或查数据库。
        open_orders = tuple(open_orders)
        checks: list[RiskCheck] = []
        notional_usdc = _intent_notional_usdc(intent)
        portfolio_total_invested_usdc = _normalize_portfolio_total(
            intent=intent,
            allocation_plan=allocation_plan,
            portfolio_total_invested_usdc=portfolio_total_invested_usdc,
        )
        open_orders_count = len(open_orders) if open_orders_count is None else open_orders_count

        decision = self._check_order_size(intent, checks, notional_usdc)
        if decision is not None:
            return decision

        decision = self._check_classification(
            intent,
            checks,
            classification_passed=classification_passed,
            classification_reason=classification_reason,
        )
        if decision is not None:
            return decision

        (
            market_active,
            market_open,
            clob_enabled,
            resolved,
            cancelled,
            archived,
        ) = _resolve_market_flags(
            market=market,
            market_active=market_active,
            market_open=market_open,
            clob_enabled=clob_enabled,
            resolved=resolved,
            cancelled=cancelled,
            archived=archived,
        )

        decision = self._check_market_state(
            intent,
            checks,
            market=market,
            position=position,
            market_active=market_active,
            market_open=market_open,
            clob_enabled=clob_enabled,
            resolved=resolved,
            cancelled=cancelled,
            archived=archived,
        )
        if decision is not None:
            return decision

        decision = self._check_price_and_tick(intent, checks, market=market, orderbook=orderbook)
        if decision is not None:
            return decision

        market_min_order_size = _effective_min_order_size(market=market, min_order_size=min_order_size)
        decision = self._check_min_order_size(
            intent,
            checks,
            notional_usdc=notional_usdc,
            market_min_order_size=market_min_order_size,
        )
        if decision is not None:
            return decision

        decision = self._check_kelly_constraints(
            intent,
            checks,
            notional_usdc=notional_usdc,
            portfolio_total_invested_usdc=portfolio_total_invested_usdc,
            bankroll_usdc=bankroll_usdc,
            kelly_max_position_fraction=kelly_max_position_fraction,
            kelly_round_up_max_overbet_ratio=kelly_round_up_max_overbet_ratio,
            peak_bankroll_usdc=peak_bankroll_usdc,
            kelly_drawdown_halt_fraction=kelly_drawdown_halt_fraction,
        )
        if decision is not None:
            return decision

        decision = self._check_operational_limits(
            intent,
            checks,
            retry_count=retry_count,
            order_retry_limit=order_retry_limit,
        )
        if decision is not None:
            return decision

        decision = self._check_account_gates(
            intent,
            checks,
            market=market,
            notional_usdc=notional_usdc,
            geoblocked=geoblocked,
            balance_usdc=balance_usdc,
            allowance_usdc=allowance_usdc,
        )
        if decision is not None:
            return decision

        decision = self._check_buy_order_type(intent, checks)
        if decision is not None:
            return decision

        decision = self._check_liquidity(
            intent,
            checks,
            orderbook=orderbook,
            notional_usdc=notional_usdc,
        )
        if decision is not None:
            return decision

        decision = self._check_open_buy_orders(intent, checks, open_orders)
        if decision is not None:
            return decision

        decision = self._check_open_exit_orders(intent, checks, open_orders, position=position)
        if decision is not None:
            return decision

        return RiskDecision(
            trace_id=intent.trace_id,
            passed=True,
            reason="passed",
            suggested_action="submit",
            retryable=False,
            checks=tuple(checks),
        )

    def _check_order_size(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        notional_usdc: Decimal,
    ) -> RiskDecision | None:
        if notional_usdc <= Decimal("0"):
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="order_size_gate",
                reason="missing_order_size",
                field="intent.amount_usdc",
                suggested_action="reject",
                retryable=False,
            )
        checks.append(
            RiskCheck(
                name="order_size_gate",
                passed=True,
                field=(
                    "intent.amount_usdc"
                    if intent.amount_usdc is not None
                    else "intent.size_shares"
                ),
                value=notional_usdc,
            )
        )
        return None

    def _check_classification(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        classification_passed: bool | None,
        classification_reason: str | None,
    ) -> RiskDecision | None:
        if classification_passed is False:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="classification_gate",
                reason=classification_reason or "market_out_of_universe",
                field="classification",
                suggested_action="reject",
                retryable=False,
            )
        checks.append(
            RiskCheck(
                name="classification_gate",
                passed=True,
                field="classification",
                value=classification_passed if classification_passed is not None else True,
            )
        )
        return None

    def _check_market_state(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        market: Market | None,
        position: Position | None,
        market_active: bool,
        market_open: bool,
        clob_enabled: bool,
        resolved: bool,
        cancelled: bool,
        archived: bool,
    ) -> RiskDecision | None:
        candidate_position_reducing_sell = _is_candidate_position_reducing_sell(
            intent,
            market=market,
            position=position,
        )
        for name, passed, reason, field_name, suggested_action in (
            (
                "market_active_gate",
                market_active or candidate_position_reducing_sell,
                "market_not_active",
                "market.active",
                "refresh_snapshot",
            ),
            (
                "market_open_gate",
                market_open or candidate_position_reducing_sell,
                "market_not_open",
                "market.open",
                "refresh_snapshot",
            ),
            (
                "clob_gate",
                clob_enabled,
                "clob_disabled",
                "market.clob_enabled",
                "refresh_snapshot",
            ),
            ("resolved_gate", not resolved, "market_resolved", "market.resolved", "reject"),
            ("cancelled_gate", not cancelled, "market_cancelled", "market.cancelled", "reject"),
            ("archived_gate", not archived, "market_archived", "market.archived", "reject"),
        ):
            checks.append(RiskCheck(name=name, passed=bool(passed), field=field_name, value=passed))
            if not passed:
                return self._fail(
                    trace_id=intent.trace_id,
                    checks=checks,
                    name=name,
                    reason=reason,
                    field=field_name,
                    suggested_action=suggested_action,
                    retryable=False,
            )

        if market is not None and market.trading_status != TradingStatus.ELIGIBLE:
            if candidate_position_reducing_sell:
                checks.append(
                    RiskCheck(
                        name="market_status_gate",
                        passed=True,
                        field="market.trading_status",
                        value={
                            "status": market.trading_status,
                            "allowed_for_position_reducing_sell": True,
                        },
                    )
                )
                return None
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="market_status_gate",
                reason="market_not_active",
                field="market.trading_status",
                value=market.trading_status,
                suggested_action="reject",
                retryable=False,
            )
        return None

    def _check_price_and_tick(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        market: Market | None,
        orderbook: OrderbookSnapshot | None,
    ) -> RiskDecision | None:
        if intent.price <= Decimal("0"):
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="price_gate",
                reason="price_invalid",
                field="intent.price",
                value=intent.price,
                suggested_action="reject",
                retryable=False,
            )

        tick_size = _effective_tick_size(market=market, orderbook=orderbook)
        if tick_size is not None:
            max_price = Decimal("1") - tick_size
            if intent.price > max_price:
                return self._fail(
                    trace_id=intent.trace_id,
                    checks=checks,
                    name="price_tick_limit_gate",
                    reason="price_above_tick_limit",
                    field="intent.price",
                    value={"price": intent.price, "tick_size": tick_size, "max_price": max_price},
                    suggested_action="reject",
                    retryable=False,
                )
            if tick_size <= Decimal("0") or (
                not _is_multiple_of_tick(intent.price, tick_size)
                and not _is_sell_endpoint_price(intent)
            ):
                return self._fail(
                    trace_id=intent.trace_id,
                    checks=checks,
                    name="tick_size_gate",
                    reason="tick_size_invalid",
                    field="intent.price",
                    value={"price": intent.price, "tick_size": tick_size},
                    suggested_action="reject",
                    retryable=False,
                )
        checks.append(
            RiskCheck(
                name="tick_size_gate",
                passed=True,
                field="intent.price",
                value={"price": intent.price, "tick_size": tick_size},
            )
        )
        return None

    def _check_min_order_size(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        notional_usdc: Decimal,
        market_min_order_size: Decimal,
    ) -> RiskDecision | None:
        if notional_usdc < _MIN_CLOB_NOTIONAL_USDC:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="min_notional_gate",
                reason="min_notional_not_met",
                field="intent.amount_usdc" if intent.side == OrderSide.BUY else "intent.size_shares",
                value={
                    "notional_usdc": notional_usdc,
                    "min_notional_usdc": _MIN_CLOB_NOTIONAL_USDC,
                },
                suggested_action="skip_dust_order",
                retryable=False,
            )
        order_size_shares = _intent_order_size_shares(intent)
        if order_size_shares < market_min_order_size:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="min_order_gate",
                reason="min_order_not_met",
                field=(
                    "intent.amount_usdc/intent.price"
                    if intent.side == OrderSide.BUY
                    else "intent.size_shares"
                ),
                value={
                    "order_size_shares": order_size_shares,
                    "min_order_size": market_min_order_size,
                    "notional_usdc": notional_usdc,
                },
                suggested_action="reject",
                retryable=False,
            )
        checks.append(
            RiskCheck(
                name="min_order_gate",
                passed=True,
                field=(
                    "intent.amount_usdc/intent.price"
                    if intent.side == OrderSide.BUY
                    else "intent.size_shares"
                ),
                value={
                    "order_size_shares": order_size_shares,
                    "min_order_size": market_min_order_size,
                    "notional_usdc": notional_usdc,
                },
            )
        )
        return None

    def _check_kelly_constraints(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        notional_usdc: Decimal,
        portfolio_total_invested_usdc: Decimal,
        bankroll_usdc: Decimal | None,
        kelly_max_position_fraction: Decimal | None,
        kelly_round_up_max_overbet_ratio: Decimal | None,
        peak_bankroll_usdc: Decimal | None,
        kelly_drawdown_halt_fraction: Decimal | None,
    ) -> RiskDecision | None:
        """Kelly 框架风控：替代旧 max_order/market/total_usdc 三层硬上限。

        三道闸：
        1. drawdown lockout：bankroll < peak × halt_fraction 时拒任意新仓（含 BUY/SELL 不区分）。
        2. single position cap：单笔 BUY notional ≤ bankroll × kelly_max_position_fraction。
        3. total exposure：已开 + 本笔 ≤ bankroll。

        Kelly 自身的 (p,c,edge,f*,min_edge,round-up) 由 EntryPlanner / 策略侧 ``kelly_stake``
        实现并写入 ``Allocation``。RiskManager 只做 caller 已经服从 Kelly 公式的最终边界校验，
        防止策略侧旁路 Kelly 公式直接送 over-bet 的 intent。
        """

        # drawdown lockout 在 BUY/SELL 之前就要拒，否则 SELL 可能放大问题。
        if (
            kelly_drawdown_halt_fraction is not None
            and kelly_drawdown_halt_fraction > Decimal("0")
            and peak_bankroll_usdc is not None
            and peak_bankroll_usdc > Decimal("0")
            and bankroll_usdc is not None
        ):
            halt_threshold = peak_bankroll_usdc * kelly_drawdown_halt_fraction
            if bankroll_usdc < halt_threshold and intent.side == OrderSide.BUY:
                return self._fail(
                    trace_id=intent.trace_id,
                    checks=checks,
                    name="drawdown_lockout_gate",
                    reason="drawdown_lockout_active",
                    field="bankroll.drawdown",
                    value={
                        "bankroll_usdc": bankroll_usdc,
                        "peak_bankroll_usdc": peak_bankroll_usdc,
                        "halt_threshold": halt_threshold,
                        "halt_fraction": kelly_drawdown_halt_fraction,
                    },
                    suggested_action="wait_recovery_or_manual_review",
                    retryable=False,
                )

        if intent.side != OrderSide.BUY:
            checks.append(
                RiskCheck(
                    name="buy_budget_gate",
                    passed=True,
                    field="intent.side",
                    value={"side": intent.side.value, "budget_consuming": False},
                )
            )
            return None

        if bankroll_usdc is None or bankroll_usdc <= Decimal("0"):
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="bankroll_gate",
                reason="bankroll_non_positive",
                field="bankroll_usdc",
                value={"bankroll_usdc": bankroll_usdc},
                suggested_action="fund_or_reduce_budget",
                retryable=False,
            )

        if kelly_max_position_fraction is not None and kelly_max_position_fraction > Decimal("0"):
            # Kelly engine 在 round-up 路径里允许单笔 over-bet 到
            # ``position_cap × round_up_max_overbet_ratio``；RiskManager 这条线必须同步
            # 放宽，否则 Kelly 接受的合法 round-up 在风控层会被拒，造成不一致。
            overbet_ratio = (
                kelly_round_up_max_overbet_ratio
                if kelly_round_up_max_overbet_ratio is not None
                and kelly_round_up_max_overbet_ratio > Decimal("0")
                else Decimal("1")
            )
            position_cap = bankroll_usdc * kelly_max_position_fraction * overbet_ratio
            if notional_usdc > position_cap:
                return self._fail(
                    trace_id=intent.trace_id,
                    checks=checks,
                    name="kelly_position_cap_gate",
                    reason="kelly_position_cap_exceeded",
                    field="intent.amount_usdc",
                    value={
                        "notional_usdc": notional_usdc,
                        "bankroll_usdc": bankroll_usdc,
                        "max_position_fraction": kelly_max_position_fraction,
                        "round_up_max_overbet_ratio": overbet_ratio,
                        "position_cap_usdc": position_cap,
                    },
                    suggested_action="reduce_size",
                    retryable=False,
                )

        if portfolio_total_invested_usdc + notional_usdc > bankroll_usdc:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="bankroll_total_gate",
                reason="bankroll_overspent",
                field="portfolio.total_invested_usdc",
                value={
                    "portfolio_total_invested_usdc": portfolio_total_invested_usdc,
                    "notional_usdc": notional_usdc,
                    "bankroll_usdc": bankroll_usdc,
                },
                suggested_action="reduce_size",
                retryable=False,
            )
        return None

    def _check_operational_limits(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        retry_count: int,
        order_retry_limit: int | None,
    ) -> RiskDecision | None:
        if retry_count >= 0 and order_retry_limit is not None and retry_count >= order_retry_limit:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="retry_gate",
                reason="retry_limit_reached",
                field="retry_count",
                value={"retry_count": retry_count, "order_retry_limit": order_retry_limit},
                suggested_action="wait",
                retryable=False,
            )
        return None

    def _check_account_gates(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        market: Market | None,
        notional_usdc: Decimal,
        geoblocked: bool,
        balance_usdc: Decimal | None,
        allowance_usdc: Decimal | None,
    ) -> RiskDecision | None:
        if geoblocked:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="geoblock_gate",
                reason="geoblock_restricted",
                field="account.geoblocked",
                value=True,
                suggested_action="reject",
                retryable=False,
            )
        if intent.side != OrderSide.BUY:
            return None
        estimated_fee_usdc = _estimated_buy_taker_fee_usdc(intent, market=market)
        required_balance_usdc = notional_usdc + estimated_fee_usdc
        if balance_usdc is not None and required_balance_usdc > balance_usdc:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="balance_gate",
                reason="balance_insufficient",
                field="account.balance_usdc",
                value={
                    "balance_usdc": balance_usdc,
                    "notional_usdc": notional_usdc,
                    "estimated_fee_usdc": estimated_fee_usdc,
                    "required_balance_usdc": required_balance_usdc,
                },
                suggested_action="reduce_size",
                retryable=False,
            )
        if allowance_usdc is not None and required_balance_usdc > allowance_usdc:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="allowance_gate",
                reason="allowance_insufficient",
                field="account.allowance_usdc",
                value={
                    "allowance_usdc": allowance_usdc,
                    "notional_usdc": notional_usdc,
                    "estimated_fee_usdc": estimated_fee_usdc,
                    "required_allowance_usdc": required_balance_usdc,
                },
                suggested_action="approve_or_reduce",
                retryable=False,
            )
        return None

    def _check_buy_order_type(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
    ) -> RiskDecision | None:
        """禁止 long-resting BUY：BUY 只允许 FAK（IOC/FOK）或 GTC+post_only（maker）。

        CLAUDE.md §3「买入侧不得保留长期 resting BUY order」。``_check_liquidity``
        虽然在足深市场会让 marketable GTC taker BUY 通过，但价格滑动 + 部分成交
        仍可能留 resting tail。这道独立检查保证策略层不会无意中构造 GTC+post_only=False
        的 BUY，把"是否 resting"的判断收敛到订单类型本身。
        """

        if intent.side != OrderSide.BUY:
            return None
        if intent.order_type == OrderType.FAK:
            checks.append(
                RiskCheck(
                    name="buy_order_type_gate",
                    passed=True,
                    field="intent.order_type",
                    value="FAK",
                )
            )
            return None
        if intent.order_type == OrderType.GTC and intent.post_only:
            checks.append(
                RiskCheck(
                    name="buy_order_type_gate",
                    passed=True,
                    field="intent.order_type",
                    value="GTC+post_only",
                )
            )
            return None
        return self._fail(
            trace_id=intent.trace_id,
            checks=checks,
            name="buy_order_type_gate",
            reason="resting_buy_not_allowed",
            field="intent.order_type",
            value={
                "order_type": intent.order_type.value if intent.order_type else None,
                "post_only": intent.post_only,
            },
            suggested_action="use_fak_or_post_only_gtc",
            retryable=False,
        )

    def _check_liquidity(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        orderbook: OrderbookSnapshot | None,
        notional_usdc: Decimal,
    ) -> RiskDecision | None:
        if orderbook is None:
            return None
        if intent.side != OrderSide.BUY:
            return None
        if _is_post_only_maker_buy(intent):
            checks.append(
                RiskCheck(
                    name="liquidity_gate",
                    passed=True,
                    reason="post_only_maker_liquidity_not_required",
                    field="intent.post_only",
                    value={
                        "order_type": intent.order_type,
                        "post_only": intent.post_only,
                        "price": intent.price,
                    },
                )
            )
            return None
        depth_usdc = _orderbook_depth_usdc(orderbook, price_cap=intent.price)
        if depth_usdc < notional_usdc:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="liquidity_gate",
                reason="liquidity_insufficient",
                field="orderbook.depth_usdc",
                value={"depth_usdc": depth_usdc, "notional_usdc": notional_usdc},
                suggested_action="wait",
                retryable=True,
            )
        return None

    def _check_open_buy_orders(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        open_orders: tuple[Order, ...],
    ) -> RiskDecision | None:
        open_buy_orders = _open_buy_orders_for_subject(open_orders, intent)
        if open_buy_orders:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="open_buy_gate",
                reason="open_buy_detected",
                field="open_orders.buy",
                value=[
                    order.order_id or order.idempotency_key or order.token_id
                    for order in open_buy_orders
                ],
                suggested_action="cancel_open_buy",
                retryable=False,
            )
        checks.append(
            RiskCheck(
                name="open_buy_gate",
                passed=True,
                field="open_orders.buy",
                value=0,
            )
        )
        return None

    def _check_open_exit_orders(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        open_orders: tuple[Order, ...],
        *,
        position: Position | None,
    ) -> RiskDecision | None:
        # 退出卖单存在时，同 token 再次买入会把“止盈/清仓中”的仓位重新放大。
        # 即使持仓快照暂时滞后，也必须等退出订单完成或撤销后再允许新入场。
        if intent.side != OrderSide.BUY:
            return None
        open_exit_orders = _open_sell_orders_for_subject(open_orders, intent)
        if open_exit_orders and getattr(intent, "allow_open_exit_overlap", False):
            covered_shares = _covered_sell_shares(position, open_exit_orders)
            position_shares = Decimal("0") if position is None else position.shares
            if position is None or position_shares <= Decimal("0") or covered_shares < position_shares:
                return self._fail(
                    trace_id=intent.trace_id,
                    checks=checks,
                    name="open_exit_gate",
                    reason="open_exit_overlap_without_covered_position",
                    field="open_orders.sell",
                    value={
                        "position_shares": position_shares,
                        "covered_shares": covered_shares,
                    },
                    suggested_action="wait_exit",
                    retryable=False,
                )
            checks.append(
                RiskCheck(
                    name="open_exit_gate",
                    passed=True,
                    field="open_orders.sell",
                    value={
                        "allowed_for_controlled_scale_in": True,
                        "position_shares": position_shares,
                        "covered_shares": covered_shares,
                        "orders": [
                            order.order_id or order.idempotency_key or order.token_id
                            for order in open_exit_orders
                        ],
                    },
                )
            )
            return None
        if open_exit_orders:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="open_exit_gate",
                reason="open_exit_detected",
                field="open_orders.sell",
                value=[
                    order.order_id or order.idempotency_key or order.token_id
                    for order in open_exit_orders
                ],
                suggested_action="wait_exit",
                retryable=False,
            )
        checks.append(
            RiskCheck(
                name="open_exit_gate",
                passed=True,
                field="open_orders.sell",
                value=0,
            )
        )
        return None

    def _fail(
        self,
        *,
        trace_id: str,
        checks: list[RiskCheck],
        name: str,
        reason: str,
        field: str | None,
        suggested_action: str,
        retryable: bool,
        value: object | None = None,
    ) -> RiskDecision:
        checks.append(
            RiskCheck(
                name=name,
                passed=False,
                reason=reason,
                field=field,
                value=value,
                suggested_action=suggested_action,
                retryable=retryable,
            )
        )
        return RiskDecision(
            trace_id=trace_id,
            passed=False,
            reason=reason,
            failed_field=field,
            suggested_action=suggested_action,
            retryable=retryable,
            checks=tuple(checks),
        )


def _normalize_portfolio_total(
    *,
    intent: OrderIntent,
    allocation_plan: AllocationPlan | None,
    portfolio_total_invested_usdc: Decimal | None,
) -> Decimal:
    if portfolio_total_invested_usdc is None and allocation_plan is not None:
        portfolio_total_invested_usdc = allocation_plan.allocated_budget_usdc - _intent_notional_usdc(
            intent
        )
    if portfolio_total_invested_usdc is None:
        portfolio_total_invested_usdc = Decimal("0")
    if portfolio_total_invested_usdc < Decimal("0"):
        return Decimal("0")
    return portfolio_total_invested_usdc


def _intent_notional_usdc(intent: OrderIntent) -> Decimal:
    if intent.notional_usdc is not None:
        return intent.notional_usdc
    if intent.amount_usdc is not None:
        return intent.amount_usdc
    if intent.size_shares is not None:
        return intent.price * intent.size_shares
    return Decimal("0")


def _intent_order_size_shares(intent: OrderIntent) -> Decimal:
    """把订单意图换算为 Polymarket CLOB 校验的 size 口径。

    CLOB 的 `min_order_size` 是条件代币份额下限。BUY market order 传入的是
    USDC amount，因此风控要用 `amount_usdc / price` 还原为份额；SELL 本身
    直接传 size_shares。
    """

    if intent.size_shares is not None:
        return max(intent.size_shares, Decimal("0"))
    if intent.amount_usdc is not None and intent.price > Decimal("0"):
        return intent.amount_usdc / intent.price
    return Decimal("0")


def _is_candidate_position_reducing_sell(
    intent: OrderIntent,
    *,
    market: Market | None,
    position: Position | None,
) -> bool:
    """允许已有持仓在仅有本地候选快照时继续提交减仓 SELL。

    CANDIDATE 表示本地发现/恢复链路尚未拿到完整市场元数据，不等同于权威终态。
    该放行只适用于已持仓且 size 被持仓覆盖的 SELL；BUY 和无持仓 SELL 仍按
    常规 market gate 拒绝。
    """

    if intent.side != OrderSide.SELL:
        return False
    if market is None or market.trading_status != TradingStatus.CANDIDATE:
        return False
    if position is None:
        return False
    if position.condition_id != intent.condition_id or position.token_id != intent.token_id:
        return False
    order_size_shares = _intent_order_size_shares(intent)
    return order_size_shares > Decimal("0") and position.shares >= order_size_shares


def _is_sell_endpoint_price(intent: OrderIntent) -> bool:
    """Polymarket CLOB 接受 0.99 作为价格上限端点，SELL 退出允许使用该价。"""

    return intent.side == OrderSide.SELL and intent.price == Decimal("0.99")


def _is_post_only_maker_buy(intent: OrderIntent) -> bool:
    """识别被动买入挂单；这类订单等待对手盘成交，不使用当前 ask 深度做成交性门禁。"""

    return (
        intent.side == OrderSide.BUY
        and intent.order_type == OrderType.GTC
        and intent.post_only
    )


def _estimated_buy_taker_fee_usdc(intent: OrderIntent, *, market: Market | None) -> Decimal:
    """预估 BUY taker 订单需要额外预留的费用，避免余额刚好等于订单金额时被 CLOB 拒绝。"""

    if market is None or intent.side != OrderSide.BUY or _is_post_only_maker_buy(intent):
        return Decimal("0")
    fee_rate_bps = _effective_taker_fee_rate_bps(market)
    if fee_rate_bps is None or fee_rate_bps <= 0 or intent.amount_usdc is None or intent.price <= Decimal("0"):
        return Decimal("0")
    fee_quote = calculate_trade_fee(
        price=intent.price,
        size_shares=intent.amount_usdc / intent.price,
        side="buy",
        fee_rate_bps=fee_rate_bps,
        fees_enabled=market.fees_enabled,
        liquidity_role="taker",
    )
    return fee_quote.fee_usdc


def _effective_taker_fee_rate_bps(market: Market) -> int | None:
    if market.fees_enabled is False:
        return 0
    if market.fee_rate_bps is not None:
        return market.fee_rate_bps
    return market.taker_base_fee_bps


def _resolve_market_flags(
    *,
    market: Market | None,
    market_active: bool | None,
    market_open: bool | None,
    clob_enabled: bool | None,
    resolved: bool | None,
    cancelled: bool | None,
    archived: bool | None,
) -> tuple[bool, bool, bool, bool, bool, bool]:
    if market is not None:
        if market_active is None:
            market_active = market.trading_status == TradingStatus.ELIGIBLE
        if market_open is None:
            market_open = market.trading_status == TradingStatus.ELIGIBLE
        if resolved is None:
            resolved = market.trading_status == TradingStatus.RESOLVED
    market_active = True if market_active is None else market_active
    market_open = True if market_open is None else market_open
    clob_enabled = True if clob_enabled is None else clob_enabled
    resolved = False if resolved is None else resolved
    cancelled = False if cancelled is None else cancelled
    archived = False if archived is None else archived
    return market_active, market_open, clob_enabled, resolved, cancelled, archived


def _effective_min_order_size(
    *,
    market: Market | None,
    min_order_size: Decimal | None,
) -> Decimal:
    # Polymarket 字段是 market 级交易参数；min_order_size 参数是框架调用侧
    # 额外传入的本地保护下限。两者不是同一个配置来源。
    market_min_order_size = market.min_order_size if market is not None else min_order_size
    if market_min_order_size is None:
        market_min_order_size = Decimal("0")
    if min_order_size is not None and market_min_order_size < min_order_size:
        return min_order_size
    return market_min_order_size


def _effective_tick_size(
    *,
    market: Market | None,
    orderbook: OrderbookSnapshot | None,
) -> Decimal | None:
    if orderbook is not None and orderbook.tick_size is not None:
        return orderbook.tick_size
    if market is not None:
        return market.tick_size
    return None


def _is_multiple_of_tick(price: Decimal, tick_size: Decimal) -> bool:
    if tick_size <= Decimal("0"):
        return False
    remainder = price % tick_size
    return remainder == Decimal("0")


def _orderbook_depth_usdc(orderbook: OrderbookSnapshot, *, price_cap: Decimal) -> Decimal:
    depth_usdc = Decimal("0")
    for level in orderbook.asks:
        if level.price <= price_cap:
            depth_usdc += level.price * level.size
    if (
        depth_usdc == Decimal("0")
        and orderbook.best_ask is not None
        and orderbook.best_ask_size is not None
    ):
        if orderbook.best_ask <= price_cap:
            return orderbook.best_ask * orderbook.best_ask_size
    return depth_usdc




def _open_buy_orders_for_subject(
    open_orders: tuple[Order, ...],
    intent: OrderIntent,
) -> tuple[Order, ...]:
    subject_orders: list[Order] = []
    for order in open_orders:
        if order.condition_id != intent.condition_id:
            continue
        if order.token_id != intent.token_id:
            continue
        if order.side != OrderSide.BUY:
            continue
        if order.status in {
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
            OrderStatus.NO_FILL,
            OrderStatus.MATCHED,
        }:
            continue
        subject_orders.append(order)
    return tuple(subject_orders)


def _open_sell_orders_for_subject(
    open_orders: tuple[Order, ...],
    intent: OrderIntent,
) -> tuple[Order, ...]:
    subject_orders: list[Order] = []
    for order in open_orders:
        if order.condition_id != intent.condition_id:
            continue
        if order.token_id != intent.token_id:
            continue
        if order.side != OrderSide.SELL:
            continue
        if order.status in {
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
            OrderStatus.NO_FILL,
            OrderStatus.MATCHED,
        }:
            continue
        subject_orders.append(order)
    return tuple(subject_orders)


def _covered_sell_shares(position: Position | None, open_exit_orders: tuple[Order, ...]) -> Decimal:
    position_covered = Decimal("0") if position is None else position.open_sell_shares
    order_covered = Decimal("0")
    for order in open_exit_orders:
        if order.remaining_shares is not None:
            order_covered += order.remaining_shares
        elif order.size_shares is not None:
            order_covered += order.size_shares
    return max(position_covered, order_covered)
